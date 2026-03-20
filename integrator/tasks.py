import json
import time
import hashlib
import requests
import random
from .eshop_api_con import API_URL, headers
from .erp_data_quality import validate_items, consistent_items
from unittest.mock import patch, Mock
from collections import defaultdict
from celery import shared_task, group
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from .models import ProductSync, DataQualityLog


import logging

logger = logging.getLogger(__name__)

class MockResponse:
    def __init__(self, status_code):
        self.status_code = status_code

def get_erp_data(file_name):
    """
    Load data function: Open data from local disc
    """

    try:
        with open(f"{file_name}.json") as f:
            data = json.load(f)
            return data
    except FileNotFoundError:
        raise Exception("ERP file not found")


def transform_erp_data(data):
    """
    Transformation function: 
    1) adds 21% VAT to variable price_vat_excl, 
    2) aggregate (sums) stocks if, there are more dicts (in list of dicts) with same ID
    3) sets color attributes to {'color': "N/A"} if missing
    """
     
    transformed_data = {}
    
    for item in data:
        sku_id = item['id']
    
        if sku_id not in transformed_data:
            transformed_data[sku_id] = {
                'id': sku_id,
                'title': item['title'],
                #'price_vat_excl': 0,
                'price_vat': 0,
                'stocks': defaultdict(int),
                'attributes': {
                    'color': (item.get("attributes") or {}).get("color") or "N/A"
                }
            }
        # Add VAT to price_vat_excl and save it variable price_vat
        price = item.get('price_vat_excl')
        if price is not None and price > 0:
            transformed_data[sku_id]['price_vat'] = price * 1.21 
        elif price is None or price == 0:
            transformed_data[sku_id]['price_vat'] = None
            
    
        # Aggregate stocks for each location
        stocks = item.get('stocks', {})
        for location, qty in stocks.items():
            if isinstance(qty, (int, float)):
                transformed_data[sku_id]['stocks'][location] += qty


    for sku in transformed_data.values():
        sku['stocks'] = dict(sku['stocks'])
    
    return transformed_data

def get_hash(data):
    """
    Hash function: Create hashes for each dict in list if dicts
    """
    hashes = {}
    for sku_id, item in data.items():
        json_data = json.dumps(item, sort_keys=True)
        hashes[sku_id] = {'data_hash':hashlib.sha256(json_data.encode("utf-8")).hexdigest()}
    return hashes

def preprocess_erp_data(data): 
    """
    Preprocessing data function: validation, splits dataset into invalid and valid items. Ivalid items are send to DB (SELECT * FROM integrator_dataqualitylog LIMIT 10;). Valid items are transformed and hashed. The RETURN of this function are validated, transformed, hashed data.
    """
    # -----------------------------
    # Validate ERP data
    # -----------------------------
    valid_data, invalid_data = validate_items(data) 
    valid_data, inconsistencies = consistent_items(valid_data)
    dqf_input = invalid_data | inconsistencies
    # -----------------------------
    # Transform valid data
    # -----------------------------
    transformed = transform_erp_data(valid_data)
    # -----------------------------
    # Hash invalid/inconsistent data
    # -----------------------------
    transformed_hash = get_hash(transformed)
    dqf_input_hash = get_hash(dqf_input)
    # -----------------------------
    # Save invalid/inconsistent items to DB
    # -----------------------------
    for sku_id in dqf_input:
        product_hash = dqf_input_hash[sku_id].get('data_hash')
        db_dq, created = DataQualityLog.objects.get_or_create(
                sku = sku_id,
                defaults = {"data_hash": product_hash})
        db_dq.data_hash = product_hash
        db_dq.data_dict = dqf_input[sku_id]
        db_dq.error_message = dqf_input[sku_id].get('error_message')
        db_dq.save()
        
    return transformed, transformed_hash



@shared_task(bind=True, rate_limit="5/s", max_retries=10)
def sync_single_sku(self, sku_id, product_dict, sku_hash, MOCK_API):
    """
    Sync function: check DB, based on results from this check POST/PATCH and update DB.
    SELECT * FROM integrator_productsync LIMIT 10;
    """
    try:
        # Check if SKU exists
        db_obj = ProductSync.objects.get(sku=sku_id)
        sku_exists = True
    except ProductSync.DoesNotExist:
        db_obj = None
        sku_exists = False
    
    if sku_exists:
        # SKU exists — update only if hash changed
        if db_obj.data_hash == sku_hash:
            logger.info(f"{sku_id} exists and hash matches — nothing to do.")
            return
        method = "PATCH"
        url = f"{API_URL}/products/{sku_id}/"
    else:
        # SKU does not exist — create
        method = "POST"
        url = f"{API_URL}/products/"
    
    if MOCK_API:
        if random.random() < 0.5:  # 50% chance
            response = MockResponse(429)
        elif not sku_exists:
            response = MockResponse(201)
        else:
            response = MockResponse(200)
    else:
        # Mock set to 429 to test retry mechanism
        mock = Mock()
        mock.status_code = 429
        mock.raise_for_status.side_effect = requests.HTTPError("429 Too Many Requests")
        with patch("requests.request", side_effect = [mock]):
            response = requests.request(method, url, json=product_dict, headers=headers, timeout=5)
            logger.info(f"URL REQUEST {sku_id}: , {response.status_code}")

    # If HTTP response.status is set to 429, we try 10 more times to send request 
    if response.status_code == 429:
        logger.info(f"{sku_id}: got 429, retrying...")
        raise self.retry(countdown=1) 
          
    if response.status_code == 201: 
        ProductSync.objects.create(sku=sku_id, data_hash = sku_hash, data_dict = product_dict)
    elif response.status_code == 200 and db_obj:
        db_obj.data_hash = sku_hash
        db_obj.data_dict = product_dict
        db_obj.save()
    


def sync_products(file_name, MOCK_API):
    """
    Main function: loads ERP data, transforms it, and dispatches per-SKU tasks.
    """
    # Step 1: Load and preprocess ERP data (CPU-bound, synchronous)
    transformed, transformed_hash = preprocess_erp_data(get_erp_data(file_name))

    if not transformed:
        return "No SKUs to sync."
        
    # Step 2: Dispatch a Celery task per SKU
    job = group(
        sync_single_sku.s(sku_id, transformed[sku_id], transformed_hash[sku_id]['data_hash'], MOCK_API)
        for sku_id in transformed
    )
    job.apply_async()
    
    return f"Dispatched {len(transformed)} SKU tasks from {file_name}."

