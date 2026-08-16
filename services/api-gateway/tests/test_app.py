"""
Unit tests cho api-gateway.

Chạy local:
  cd services/api-gateway
  pip install -r requirements.txt pytest
  python -m pytest tests/ -v

Chạy trong CI:
  Job lint-and-test trong pr-checks.yml tự động chạy file này.

Học tập:
  - Flask test client: không cần server chạy thật, test trong process
  - Mock upstream (order-service, product-service): test gateway logic
    mà không cần 2 service kia chạy
  - ERROR_RATE testing: inject lỗi bằng env var, verify behavior
"""

import pytest
import os
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Fixtures — setup/teardown cho mỗi test
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Tạo Flask test client với ERROR_RATE=0 (không inject lỗi)."""
    # Set env trước khi import app — vì app.py đọc env khi module load
    os.environ["ERROR_RATE"] = "0"
    os.environ["VERSION"] = "test-v1"
    os.environ["ORDER_SERVICE_URL"] = "http://mock-order:8081"
    os.environ["PRODUCT_SERVICE_URL"] = "http://mock-product:8082"

    # Import sau khi set env
    import sys
    # Reload module để đọc env mới
    if "app" in sys.modules:
        del sys.modules["app"]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app import app as flask_app

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def error_client():
    """Flask test client với ERROR_RATE=1 (luôn inject lỗi)."""
    os.environ["ERROR_RATE"] = "1"
    os.environ["VERSION"] = "test-v1"

    import sys
    if "app" in sys.modules:
        del sys.modules["app"]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app import app as flask_app

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c
    # Cleanup
    os.environ["ERROR_RATE"] = "0"


# ---------------------------------------------------------------------------
# Test /healthz — readinessProbe endpoint
# ---------------------------------------------------------------------------

class TestHealthz:
    def test_healthz_returns_200(self, client):
        """K8s readinessProbe expect 200."""
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_returns_ok_status(self, client):
        resp = client.get("/healthz")
        data = resp.get_json()
        assert data["status"] == "ok"

    def test_healthz_returns_version(self, client):
        resp = client.get("/healthz")
        data = resp.get_json()
        assert "version" in data
        assert data["version"] == "test-v1"

    def test_healthz_never_injects_error(self, error_client):
        """Dù ERROR_RATE=1, /healthz phải luôn trả 200.
        Quan trọng: nếu /healthz trả 500, K8s restart pod liên tục."""
        # api-gateway/app.py không gọi maybe_inject_error() trong healthz
        resp = error_client.get("/healthz")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test / (root endpoint)
# ---------------------------------------------------------------------------

class TestRoot:
    def test_root_returns_200_without_error(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_root_returns_service_info(self, client):
        resp = client.get("/")
        data = resp.get_json()
        assert data["service"] == "api-gateway"
        assert "routes" in data

    def test_root_returns_500_when_error_injected(self, error_client):
        """ERROR_RATE=1 → 100% request lỗi → phải trả 500."""
        resp = error_client.get("/")
        assert resp.status_code == 500

    def test_root_error_response_has_version(self, error_client):
        """Kể cả khi lỗi, response vẫn có version để debug."""
        resp = error_client.get("/")
        data = resp.get_json()
        assert "version" in data


# ---------------------------------------------------------------------------
# Test /products — proxy tới product-service
# Mock requests.get để không cần product-service thật chạy
# ---------------------------------------------------------------------------

class TestProducts:
    @patch("requests.get")
    def test_list_products_proxies_to_product_service(self, mock_get, client):
        """Gateway phải gọi product-service và trả về response của nó."""
        # Tạo mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "products": [{"id": 1, "name": "Keyboard"}],
            "total": 1
        }
        mock_get.return_value = mock_response

        resp = client.get("/products")

        assert resp.status_code == 200
        # Verify gateway đã gọi đúng URL của product-service
        mock_get.assert_called_once()
        call_args = mock_get.call_args[0][0]
        assert "mock-product" in call_args
        assert "/products" in call_args

    @patch("requests.get")
    def test_product_upstream_timeout_returns_503(self, mock_get, client):
        """Khi product-service timeout → gateway trả 503, không crash."""
        import requests as req
        mock_get.side_effect = req.exceptions.Timeout("timeout")

        resp = client.get("/products")

        assert resp.status_code == 503
        data = resp.get_json()
        assert "upstream" in data
        assert data["upstream"] == "product-service"

    @patch("requests.get")
    def test_get_product_by_id(self, mock_get, client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": 1, "name": "Keyboard", "price": 29.99}
        mock_get.return_value = mock_response

        resp = client.get("/products/1")
        assert resp.status_code == 200

    def test_products_return_500_when_error_injected(self, error_client):
        """ERROR_RATE=1 → không gọi upstream, trả 500 ngay."""
        resp = error_client.get("/products")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Test /orders — proxy tới order-service
# ---------------------------------------------------------------------------

class TestOrders:
    @patch("requests.get")
    def test_list_orders(self, mock_get, client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"orders": [], "total": 0}
        mock_get.return_value = mock_response

        resp = client.get("/orders")
        assert resp.status_code == 200

    @patch("requests.post")
    def test_create_order_proxies_to_order_service(self, mock_post, client):
        """POST /orders phải forward body JSON sang order-service."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": "ord-abc123",
            "product_id": 1,
            "quantity": 2,
            "status": "pending"
        }
        mock_post.return_value = mock_response

        resp = client.post(
            "/orders",
            json={"product_id": 1, "quantity": 2},
            content_type="application/json"
        )

        assert resp.status_code == 201
        data = resp.get_json()
        assert data["status"] == "pending"
        # Verify gateway đã gọi order-service
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "mock-order" in call_url

    @patch("requests.get")
    def test_order_upstream_connection_error_returns_503(self, mock_get, client):
        import requests as req
        mock_get.side_effect = req.exceptions.ConnectionError("refused")

        resp = client.get("/orders")
        assert resp.status_code == 503
        assert resp.get_json()["upstream"] == "order-service"
