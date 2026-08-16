"""
Unit tests cho order-service.

Tập trung vào:
  - CRUD đơn hàng (create, read, filter)
  - State machine (valid/invalid status transitions)
  - Input validation (product_id required, quantity phải là int dương)
  - ERROR_RATE injection
"""

import pytest
import os
import sys


@pytest.fixture(autouse=True)
def reset_orders():
    """Reset ORDERS dict trước mỗi test để test độc lập nhau."""
    # autouse=True: tự apply cho mọi test trong file
    os.environ["ERROR_RATE"] = "0"
    os.environ["VERSION"] = "test-v1"

    if "app" in sys.modules:
        del sys.modules["app"]

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    yield


@pytest.fixture
def client():
    from app import app as flask_app, ORDERS
    # Reset in-memory store
    ORDERS.clear()
    ORDERS["ord-001"] = {
        "id": "ord-001",
        "product_id": 1,
        "quantity": 2,
        "status": "confirmed",
        "total_price": 59.98,
        "created_at": "2026-01-01T00:00:00Z",
    }
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthz:
    def test_returns_200(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_includes_order_count(self, client):
        data = client.get("/healthz").get_json()
        assert "order_count" in data
        assert data["order_count"] >= 1


# ---------------------------------------------------------------------------
# GET /orders
# ---------------------------------------------------------------------------

class TestListOrders:
    def test_returns_existing_orders(self, client):
        resp = client.get("/orders")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 1

    def test_filter_by_status(self, client):
        resp = client.get("/orders?status=confirmed")
        data = resp.get_json()
        # Tất cả order trong kết quả phải có status=confirmed
        for order in data["orders"]:
            assert order["status"] == "confirmed"

    def test_filter_by_nonexistent_status_returns_empty(self, client):
        resp = client.get("/orders?status=shipped")
        data = resp.get_json()
        assert data["total"] == 0
        assert data["orders"] == []


# ---------------------------------------------------------------------------
# GET /orders/<id>
# ---------------------------------------------------------------------------

class TestGetOrder:
    def test_get_existing_order(self, client):
        resp = client.get("/orders/ord-001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == "ord-001"

    def test_get_nonexistent_order_returns_404(self, client):
        resp = client.get("/orders/ord-999")
        assert resp.status_code == 404

    def test_404_response_has_error_message(self, client):
        resp = client.get("/orders/ord-does-not-exist")
        data = resp.get_json()
        assert "error" in data


# ---------------------------------------------------------------------------
# POST /orders — Tạo đơn hàng
# ---------------------------------------------------------------------------

class TestCreateOrder:
    def test_create_order_returns_201(self, client):
        resp = client.post("/orders", json={"product_id": 1, "quantity": 2})
        assert resp.status_code == 201

    def test_create_order_has_required_fields(self, client):
        resp = client.post("/orders", json={"product_id": 1, "quantity": 1})
        data = resp.get_json()
        assert "id" in data
        assert "status" in data
        assert "total_price" in data
        assert "created_at" in data

    def test_new_order_status_is_pending(self, client):
        """Đơn hàng mới luôn bắt đầu ở trạng thái pending."""
        resp = client.post("/orders", json={"product_id": 2, "quantity": 1})
        data = resp.get_json()
        assert data["status"] == "pending"

    def test_create_order_calculates_total_price(self, client):
        """product_id=1, giá=29.99, quantity=3 → total=89.97"""
        resp = client.post("/orders", json={"product_id": 1, "quantity": 3})
        data = resp.get_json()
        assert data["total_price"] == pytest.approx(89.97, rel=1e-2)

    def test_create_order_without_product_id_returns_400(self, client):
        """product_id là required field."""
        resp = client.post("/orders", json={"quantity": 2})
        assert resp.status_code == 400

    def test_create_order_with_zero_quantity_returns_400(self, client):
        resp = client.post("/orders", json={"product_id": 1, "quantity": 0})
        assert resp.status_code == 400

    def test_create_order_with_negative_quantity_returns_400(self, client):
        resp = client.post("/orders", json={"product_id": 1, "quantity": -1})
        assert resp.status_code == 400

    def test_create_order_with_string_quantity_returns_400(self, client):
        resp = client.post("/orders", json={"product_id": 1, "quantity": "two"})
        assert resp.status_code == 400

    def test_created_order_is_retrievable(self, client):
        """Order vừa tạo phải có thể GET ngay sau đó."""
        create_resp = client.post("/orders", json={"product_id": 1, "quantity": 1})
        order_id = create_resp.get_json()["id"]

        get_resp = client.get(f"/orders/{order_id}")
        assert get_resp.status_code == 200
        assert get_resp.get_json()["id"] == order_id


# ---------------------------------------------------------------------------
# PUT /orders/<id>/status — State machine
# ---------------------------------------------------------------------------

class TestUpdateStatus:
    def test_update_to_valid_status(self, client):
        """confirmed → shipped là transition hợp lệ."""
        resp = client.put("/orders/ord-001/status", json={"status": "shipped"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "shipped"

    def test_update_to_cancelled(self, client):
        resp = client.put("/orders/ord-001/status", json={"status": "cancelled"})
        assert resp.status_code == 200

    def test_update_invalid_status_returns_400(self, client):
        """Status không nằm trong VALID_STATUSES → 400."""
        resp = client.put("/orders/ord-001/status", json={"status": "refunded"})
        assert resp.status_code == 400

    def test_update_nonexistent_order_returns_404(self, client):
        resp = client.put("/orders/ord-999/status", json={"status": "shipped"})
        assert resp.status_code == 404

    def test_updated_at_is_set_after_status_change(self, client):
        """updated_at phải được set sau khi update status."""
        resp = client.put("/orders/ord-001/status", json={"status": "shipped"})
        data = resp.get_json()
        assert "updated_at" in data


# ---------------------------------------------------------------------------
# ERROR_RATE injection
# ---------------------------------------------------------------------------

class TestErrorInjection:
    def test_error_rate_1_returns_500(self):
        """ERROR_RATE=1 → 100% request lỗi."""
        os.environ["ERROR_RATE"] = "1"
        if "app" in sys.modules:
            del sys.modules["app"]
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app import app as flask_app
        flask_app.config["TESTING"] = True

        with flask_app.test_client() as c:
            resp = c.get("/orders")
            assert resp.status_code == 500
            data = resp.get_json()
            assert "error" in data
            assert "version" in data    # version phải có kể cả khi lỗi

        os.environ["ERROR_RATE"] = "0"
