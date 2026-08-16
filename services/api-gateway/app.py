"""
API Gateway — Cổng vào duy nhất của hệ thống.

Nhiệm vụ:
  - Nhận request từ client (browser / load test tool)
  - Route sang order-service hoặc product-service
  - Inject ERROR_RATE để demo canary / SLO alert (giống Lab W9)
  - Expose /metrics cho Prometheus scrape
  - Expose /healthz cho readinessProbe K8s

Môi trường (đọc từ env var, inject qua ConfigMap/Secret K8s):
  ORDER_SERVICE_URL   = http://order-service:8081
  PRODUCT_SERVICE_URL = http://product-service:8082
  ERROR_RATE          = 0.0   (0.0–1.0, inject lỗi để test SLO)
  VERSION             = v1
"""

import os
import random
import requests
from flask import Flask, jsonify, request
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)

# PrometheusMetrics tự thêm endpoint /metrics
# và ghi nhận flask_http_request_total, flask_http_request_duration_seconds
metrics = PrometheusMetrics(app)

# --- Config đọc từ env (inject qua K8s ConfigMap) ---
ORDER_URL   = os.getenv("ORDER_SERVICE_URL",   "http://order-service:8081")
PRODUCT_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8082")
ERROR_RATE  = float(os.getenv("ERROR_RATE", "0"))  # 0 = không lỗi
VERSION     = os.getenv("VERSION", "v1")


# ---------------------------------------------------------------------------
# Helper: inject lỗi ngẫu nhiên — dùng khi demo SLO burn rate / canary abort
# ---------------------------------------------------------------------------
def maybe_inject_error():
    """Trả True nếu request này nên bị lỗi (theo xác suất ERROR_RATE)."""
    return random.random() < ERROR_RATE


# ---------------------------------------------------------------------------
# Health check — K8s readinessProbe gọi endpoint này
# Pod chỉ nhận traffic khi trả 200
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return jsonify(status="ok", version=VERSION), 200


# ---------------------------------------------------------------------------
# Root — trả thông tin gateway (dùng khi load test để đếm request)
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    if maybe_inject_error():
        # Trả 500 giả — Prometheus ghi nhận → SLO success rate tụt
        return jsonify(error="injected fault", version=VERSION), 500
    return jsonify(
        service="api-gateway",
        version=VERSION,
        routes=["/products", "/orders", "/orders/<id>"],
    ), 200


# ---------------------------------------------------------------------------
# PRODUCTS — proxy sang product-service
# ---------------------------------------------------------------------------
@app.get("/products")
def list_products():
    """Lấy danh sách sản phẩm."""
    if maybe_inject_error():
        return jsonify(error="gateway fault", version=VERSION), 500
    try:
        resp = requests.get(f"{PRODUCT_URL}/products", timeout=3)
        return jsonify(resp.json()), resp.status_code
    except requests.RequestException as exc:
        # Upstream không trả lời → 503 (Service Unavailable)
        return jsonify(error=str(exc), upstream="product-service"), 503


@app.get("/products/<int:product_id>")
def get_product(product_id):
    """Lấy chi tiết 1 sản phẩm."""
    if maybe_inject_error():
        return jsonify(error="gateway fault", version=VERSION), 500
    try:
        resp = requests.get(f"{PRODUCT_URL}/products/{product_id}", timeout=3)
        return jsonify(resp.json()), resp.status_code
    except requests.RequestException as exc:
        return jsonify(error=str(exc), upstream="product-service"), 503


# ---------------------------------------------------------------------------
# ORDERS — proxy sang order-service
# ---------------------------------------------------------------------------
@app.get("/orders")
def list_orders():
    """Lấy danh sách đơn hàng."""
    if maybe_inject_error():
        return jsonify(error="gateway fault", version=VERSION), 500
    try:
        resp = requests.get(f"{ORDER_URL}/orders", timeout=3)
        return jsonify(resp.json()), resp.status_code
    except requests.RequestException as exc:
        return jsonify(error=str(exc), upstream="order-service"), 503


@app.get("/orders/<order_id>")
def get_order(order_id):
    """Lấy chi tiết 1 đơn hàng."""
    if maybe_inject_error():
        return jsonify(error="gateway fault", version=VERSION), 500
    try:
        resp = requests.get(f"{ORDER_URL}/orders/{order_id}", timeout=3)
        return jsonify(resp.json()), resp.status_code
    except requests.RequestException as exc:
        return jsonify(error=str(exc), upstream="order-service"), 503


@app.post("/orders")
def create_order():
    """Tạo đơn hàng mới. Body JSON: {product_id, quantity}"""
    if maybe_inject_error():
        return jsonify(error="gateway fault", version=VERSION), 500
    try:
        payload = request.get_json(force=True)
        resp = requests.post(f"{ORDER_URL}/orders", json=payload, timeout=3)
        return jsonify(resp.json()), resp.status_code
    except requests.RequestException as exc:
        return jsonify(error=str(exc), upstream="order-service"), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
