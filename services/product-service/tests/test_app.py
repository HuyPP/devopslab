"""
Unit tests cho product-service.

Tập trung vào:
  - Catalog: list, filter, detail
  - Stock management: check, reserve, out-of-stock
  - Custom Prometheus counter: verify label values
"""

import pytest
import os
import sys


@pytest.fixture
def client():
    os.environ["ERROR_RATE"] = "0"
    os.environ["VERSION"] = "test-v1"

    if "app" in sys.modules:
        del sys.modules["app"]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app import app as flask_app, PRODUCTS

    # Reset stock về giá trị ban đầu trước mỗi test
    PRODUCTS[1]["stock"] = 100
    PRODUCTS[2]["stock"] = 50
    PRODUCTS[3]["stock"] = 20
    PRODUCTS[4]["stock"] = 200
    PRODUCTS[5]["stock"] = 30

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthz:
    def test_returns_200(self, client):
        assert client.get("/healthz").status_code == 200

    def test_includes_product_count(self, client):
        data = client.get("/healthz").get_json()
        assert data["product_count"] == 5


# ---------------------------------------------------------------------------
# GET /products
# ---------------------------------------------------------------------------

class TestListProducts:
    def test_returns_all_products(self, client):
        resp = client.get("/products")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 5

    def test_filter_by_category_peripherals(self, client):
        resp = client.get("/products?category=peripherals")
        data = resp.get_json()
        # peripherals: product 1, 2, 5 → 3 sản phẩm
        assert data["total"] == 3
        for p in data["products"]:
            assert p["category"] == "peripherals"

    def test_filter_by_nonexistent_category_returns_empty(self, client):
        resp = client.get("/products?category=nonexistent")
        data = resp.get_json()
        assert data["total"] == 0

    def test_products_have_required_fields(self, client):
        data = client.get("/products").get_json()
        for product in data["products"]:
            assert "id" in product
            assert "name" in product
            assert "price" in product
            assert "stock" in product


# ---------------------------------------------------------------------------
# GET /products/<id>
# ---------------------------------------------------------------------------

class TestGetProduct:
    def test_get_existing_product(self, client):
        resp = client.get("/products/1")
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "Mechanical Keyboard"

    def test_get_nonexistent_product_returns_404(self, client):
        resp = client.get("/products/999")
        assert resp.status_code == 404

    def test_product_has_price(self, client):
        data = client.get("/products/1").get_json()
        assert data["price"] == pytest.approx(29.99)


# ---------------------------------------------------------------------------
# GET /products/<id>/stock
# ---------------------------------------------------------------------------

class TestCheckStock:
    def test_returns_stock_info(self, client):
        resp = client.get("/products/1/stock")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "stock" in data
        assert "available" in data

    def test_available_true_when_stock_positive(self, client):
        data = client.get("/products/1/stock").get_json()
        assert data["available"] is True
        assert data["stock"] > 0

    def test_nonexistent_product_stock_returns_404(self, client):
        resp = client.get("/products/999/stock")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /products/<id>/reserve
# ---------------------------------------------------------------------------

class TestReserveStock:
    def test_reserve_reduces_stock(self, client):
        """Reserve 10 units → stock giảm từ 100 xuống 90."""
        resp = client.post("/products/1/reserve", json={"quantity": 10})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["reserved"] == 10
        assert data["remaining_stock"] == 90

    def test_reserve_all_stock(self, client):
        """Reserve toàn bộ stock → remaining_stock = 0."""
        resp = client.post("/products/1/reserve", json={"quantity": 100})
        assert resp.status_code == 200
        assert resp.get_json()["remaining_stock"] == 0

    def test_reserve_exceeds_stock_returns_409(self, client):
        """Reserve nhiều hơn stock → 409 Conflict."""
        resp = client.post("/products/1/reserve", json={"quantity": 101})
        assert resp.status_code == 409
        data = resp.get_json()
        assert "available" in data
        assert "requested" in data

    def test_reserve_nonexistent_product_returns_404(self, client):
        resp = client.post("/products/999/reserve", json={"quantity": 1})
        assert resp.status_code == 404

    def test_reserve_default_quantity_is_1(self, client):
        """Không truyền quantity → mặc định reserve 1."""
        resp = client.post("/products/1/reserve", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["reserved"] == 1
        assert data["remaining_stock"] == 99

    def test_sequential_reserves_accumulate(self, client):
        """Reserve 2 lần → stock giảm tổng cộng."""
        client.post("/products/1/reserve", json={"quantity": 30})
        client.post("/products/1/reserve", json={"quantity": 20})
        # stock ban đầu 100, reserve 30+20=50 → còn 50
        stock_resp = client.get("/products/1/stock")
        assert stock_resp.get_json()["stock"] == 50

    def test_reserve_stock_updates_prometheus_counter(self, client):
        """Custom metric product_reserve_total phải được tăng."""
        client.post("/products/1/reserve", json={"quantity": 1})
        # Verify qua /metrics endpoint
        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        metrics_text = metrics_resp.data.decode()
        # Counter phải có label status="success"
        assert 'product_reserve_total{status="success"}' in metrics_text


# ---------------------------------------------------------------------------
# ERROR_RATE injection
# ---------------------------------------------------------------------------

class TestErrorInjection:
    def test_error_rate_1_returns_500_on_list(self):
        os.environ["ERROR_RATE"] = "1"
        if "app" in sys.modules:
            del sys.modules["app"]
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app import app as flask_app
        flask_app.config["TESTING"] = True

        with flask_app.test_client() as c:
            resp = c.get("/products")
            assert resp.status_code == 500

        os.environ["ERROR_RATE"] = "0"

    def test_error_rate_1_increments_error_counter(self):
        """Khi inject lỗi trong reserve → counter tăng với label status=error."""
        os.environ["ERROR_RATE"] = "1"
        if "app" in sys.modules:
            del sys.modules["app"]
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app import app as flask_app
        flask_app.config["TESTING"] = True

        with flask_app.test_client() as c:
            c.post("/products/1/reserve", json={"quantity": 1})
            metrics = c.get("/metrics").data.decode()
            assert 'product_reserve_total{status="error"}' in metrics

        os.environ["ERROR_RATE"] = "0"
