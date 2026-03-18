import pytest
from integrator.tasks import transform_erp_data
from integrator.erp_data_quality import validate_items, consistent_items

def test_transform_erp_data():
    # Mock ERP input
    mock_data = [
        {
            "id": "SKU123",
            "title": "Test Product",
            "price_vat_excl": 100,
            "stocks": {"warehouse_1": 5, "warehouse_2": 3},
            "attributes": {"color": "red"}
        },
        {
            "id": "SKU123",
            "title": "Test Product",
            "price_vat_excl": 100,
            "stocks": {"warehouse_1": 2},
            "attributes": {"color": "red"}
        },
        {
            "id": "SKU456",
            "title": "No Price Product",
            "price_vat_excl": 0,
            "stocks": {"warehouse_1": 1},
            "attributes": {}
        },
        {
            "id": "SKU406",
            "title": "Missing color",
            "price_vat_excl": 900,
            "stocks": {"warehouse_1": 1},
            "attributes": {}
        },
        {
            "id": "SKU789",
            "title": "Missing Price Product",
            "stocks": {"warehouse_3": 7}
        },
        {
            "id": "SKU790",
            "title": "Inconsystent price",
            "price_vat_excl": 20,
            "stocks": {"warehouse_3": 7},
            "attributes": {"color": "red"}
        },
        {
            "id": "SKU790",
            "title": "Inconsystent price",
            "price_vat_excl": 10,
            "stocks": {"warehouse_3": 7},
            "attributes": {"color": "red"}
        }
    ]
    
    valid_data, invalid_data = validate_items(mock_data) 
    valid_data, inconsistencies = consistent_items(valid_data)
    result = transform_erp_data(valid_data)

    assert result["SKU123"]["price_vat"] == pytest.approx(121)
    assert result["SKU123"]["stocks"] == {"warehouse_1": 7, "warehouse_2": 3}
    assert result["SKU123"]["attributes"]["color"] == "red"
    assert result["SKU406"]["attributes"]["color"] == "N/A"
    assert "SKU456" not in result
    assert "SKU789" not in result
    assert "SKU790" not in result
    
    