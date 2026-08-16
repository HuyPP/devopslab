"""
Order Service — Quản lý đơn hàng.

Chức năng:
  - Tạo đơn hàng mới (POST /orders)
  - Lấy danh sách đơn hàng (GET /orders)
  - Lấy chi tiết đơn hàng (GET /orders/<id>)
  - Cập nhật trạng thái đơn hàng (PUT /orders/<id>/status)

Lưu ý học tập:
  - Dữ liệu lưu in-memory (dict) — đủ để demo, không cần DB
  - Trong thực tế: thay bằng PostgreSQL + connection pool
  - ERROR_RATE inject để test SLO từ phía service (không phải gateway)

Trạng thái đơn hàng (state machine):
  pending → confirmed → shipped → delivered
                      → cancelled
"""

import os
import random
import uuid
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)
metrics = PrometheusMetrics(app)

ERROR_RATE = float(os.getenv("ERROR_RATE", "0"))
VERSION    = os.getenv("VERSION", "v1")

# ---------------------------------------------------------------------------
# In-memory store — thay bằng PostgreSQL trong thực tế
# ---------------------------------------------------------------------------
# Dữ liệu mẫu để demo ngay khi khởi động
ORDERS: dict[str, dict] = {
    "ord-001": {
        "id": "ord-001",
        "product_id": 1,
        "quantity": 2,
        "status": "confirmed",
        "total_price": 59.98,
        "created_at": "2026-08-14T08:00:00Z",
    },
    "ord-002": {
        "id": "ord-002",
        "product_id": 3,
        "quantity": 1,
        "status": "pending",
        "total_price": 999.99,
        "created_at": "2026-08-14T09:30:00Z",
    },
}

# Giá sản phẩm hardcode — trong thực tế gọi product-service
PRODUCT_PRICES = {1: 29.99, 2: 49.99, 3: 999.99, 4: 9.99, 5: 199.99}

VALID_STATUSES = {"pending", "confirmed", "shipped", "delivered", "cancelled"}


def maybe_inject_error():
    return random.random() < ERROR_RATE


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return jsonify(status="ok", version=VERSION, order_count=len(ORDERS)), 200


# ---------------------------------------------------------------------------
# GET /orders — Danh sách đơn hàng
# ---------------------------------------------------------------------------
@app.get("/orders")
def list_orders():
    if maybe_inject_error():
        return jsonify(error="injected fault", version=VERSION), 500

    # Lọc theo status nếu có query param: GET /orders?status=pending
    status_filter = request.args.get("status")
    orders = list(ORDERS.values())
    if status_filter:
        orders = [o for o in orders if o["status"] == status_filter]

    return jsonify(
        orders=orders,
        total=len(orders),
        version=VERSION,
    ), 200


# ---------------------------------------------------------------------------
# GET /orders/<id> — Chi tiết đơn hàng
# ---------------------------------------------------------------------------
@app.get("/orders/<order_id>")
def get_order(order_id):
    if maybe_inject_error():
        return jsonify(error="injected fault", version=VERSION), 500

    order = ORDERS.get(order_id)
    if not order:
        return jsonify(error=f"Order {order_id} not found"), 404

    return jsonify(order), 200


# ---------------------------------------------------------------------------
# POST /orders — Tạo đơn hàng mới
# Body: { "product_id": 1, "quantity": 2 }
# ---------------------------------------------------------------------------
@app.post("/orders")
def create_order():
    if maybe_inject_error():
        return jsonify(error="injected fault", version=VERSION), 500

    body = request.get_json(force=True) or {}
    product_id = body.get("product_id")
    quantity   = body.get("quantity", 1)

    # Validate input
    if not product_id:
        return jsonify(error="product_id is required"), 400
    if not isinstance(quantity, int) or quantity < 1:
        return jsonify(error="quantity must be a positive integer"), 400

    unit_price  = PRODUCT_PRICES.get(product_id, 0.0)
    total_price = round(unit_price * quantity, 2)

    order_id = f"ord-{uuid.uuid4().hex[:8]}"
    order = {
        "id": order_id,
        "product_id": product_id,
        "quantity": quantity,
        "status": "pending",
        "unit_price": unit_price,
        "total_price": total_price,
        "created_at": now_iso(),
    }
    ORDERS[order_id] = order

    return jsonify(order), 201


# ---------------------------------------------------------------------------
# PUT /orders/<id>/status — Cập nhật trạng thái
# Body: { "status": "confirmed" }
# ---------------------------------------------------------------------------
@app.put("/orders/<order_id>/status")
def update_status(order_id):
    if maybe_inject_error():
        return jsonify(error="injected fault", version=VERSION), 500

    order = ORDERS.get(order_id)
    if not order:
        return jsonify(error=f"Order {order_id} not found"), 404

    body       = request.get_json(force=True) or {}
    new_status = body.get("status")
    if new_status not in VALID_STATUSES:
        return jsonify(
            error=f"Invalid status. Valid: {sorted(VALID_STATUSES)}"
        ), 400

    order["status"]     = new_status
    order["updated_at"] = now_iso()

    return jsonify(order), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
