"""
Product Service — Quản lý sản phẩm & tồn kho.

Chức năng:
  - Danh sách sản phẩm (GET /products)
  - Chi tiết sản phẩm (GET /products/<id>)
  - Kiểm tồn kho (GET /products/<id>/stock)
  - Trừ tồn kho (POST /products/<id>/reserve)  ← gọi từ order-service

Học tập:
  - Thêm custom Prometheus counter để đếm số lần "reserve" thành công/thất bại
    → ví dụ về business metric (khác với infra metric)
  - ERROR_RATE để demo SLO và AnalysisTemplate canary
"""

import os
import random
from flask import Flask, jsonify, request
from prometheus_client import Counter
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)
metrics = PrometheusMetrics(app)

ERROR_RATE = float(os.getenv("ERROR_RATE", "0"))
VERSION    = os.getenv("VERSION", "v1")

# ---------------------------------------------------------------------------
# Custom business metric — đây là điểm khác biệt với infra metric
# Prometheus ghi nhận qua /metrics, AnalysisTemplate có thể query metric này
# ---------------------------------------------------------------------------
# Đếm số lần reserve tồn kho thành công / thất bại
reserve_counter = Counter(
    "product_reserve_total",
    "Số lần reserve tồn kho",
    ["status"],        # label: success | out_of_stock | not_found
)

# ---------------------------------------------------------------------------
# In-memory catalog
# ---------------------------------------------------------------------------
PRODUCTS: dict[int, dict] = {
    1: {
        "id": 1,
        "name": "Mechanical Keyboard",
        "category": "peripherals",
        "price": 29.99,
        "stock": 100,
        "description": "Bàn phím cơ Cherry MX Blue",
    },
    2: {
        "id": 2,
        "name": "USB-C Hub",
        "category": "peripherals",
        "price": 49.99,
        "stock": 50,
        "description": "Hub 7 cổng USB-C cho Macbook",
    },
    3: {
        "id": 3,
        "name": "Laptop Stand",
        "category": "accessories",
        "price": 999.99,
        "stock": 20,
        "description": "Giá đỡ laptop nhôm nguyên khối",
    },
    4: {
        "id": 4,
        "name": "HDMI Cable 2m",
        "category": "cables",
        "price": 9.99,
        "stock": 200,
        "description": "Cáp HDMI 2.1 hỗ trợ 4K@120Hz",
    },
    5: {
        "id": 5,
        "name": "Wireless Mouse",
        "category": "peripherals",
        "price": 199.99,
        "stock": 30,
        "description": "Chuột không dây Logitech MX Master 3",
    },
}


def maybe_inject_error():
    return random.random() < ERROR_RATE


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return jsonify(
        status="ok",
        version=VERSION,
        product_count=len(PRODUCTS),
    ), 200


# ---------------------------------------------------------------------------
# GET /products — Danh sách sản phẩm
# Query params: category=peripherals  (lọc theo danh mục)
# ---------------------------------------------------------------------------
@app.get("/products")
def list_products():
    if maybe_inject_error():
        return jsonify(error="injected fault", version=VERSION), 500

    category = request.args.get("category")
    products = list(PRODUCTS.values())
    if category:
        products = [p for p in products if p["category"] == category]

    return jsonify(
        products=products,
        total=len(products),
        version=VERSION,
    ), 200


# ---------------------------------------------------------------------------
# GET /products/<id> — Chi tiết sản phẩm
# ---------------------------------------------------------------------------
@app.get("/products/<int:product_id>")
def get_product(product_id):
    if maybe_inject_error():
        return jsonify(error="injected fault", version=VERSION), 500

    product = PRODUCTS.get(product_id)
    if not product:
        return jsonify(error=f"Product {product_id} not found"), 404

    return jsonify(product), 200


# ---------------------------------------------------------------------------
# GET /products/<id>/stock — Kiểm tồn kho
# ---------------------------------------------------------------------------
@app.get("/products/<int:product_id>/stock")
def check_stock(product_id):
    if maybe_inject_error():
        return jsonify(error="injected fault", version=VERSION), 500

    product = PRODUCTS.get(product_id)
    if not product:
        return jsonify(error=f"Product {product_id} not found"), 404

    return jsonify(
        product_id=product_id,
        stock=product["stock"],
        available=product["stock"] > 0,
    ), 200


# ---------------------------------------------------------------------------
# POST /products/<id>/reserve — Giữ hàng khi tạo đơn
# Body: { "quantity": 2 }
# Đây là ví dụ custom metric: theo dõi business event qua Prometheus
# ---------------------------------------------------------------------------
@app.post("/products/<int:product_id>/reserve")
def reserve_stock(product_id):
    if maybe_inject_error():
        reserve_counter.labels(status="error").inc()
        return jsonify(error="injected fault", version=VERSION), 500

    product = PRODUCTS.get(product_id)
    if not product:
        reserve_counter.labels(status="not_found").inc()
        return jsonify(error=f"Product {product_id} not found"), 404

    body     = request.get_json(force=True) or {}
    quantity = body.get("quantity", 1)

    if product["stock"] < quantity:
        reserve_counter.labels(status="out_of_stock").inc()
        return jsonify(
            error="Insufficient stock",
            available=product["stock"],
            requested=quantity,
        ), 409   # Conflict

    # Giữ hàng — trừ stock
    product["stock"] -= quantity
    reserve_counter.labels(status="success").inc()

    return jsonify(
        product_id=product_id,
        reserved=quantity,
        remaining_stock=product["stock"],
    ), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082, debug=False)
