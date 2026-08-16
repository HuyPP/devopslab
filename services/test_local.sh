#!/usr/bin/env bash
# test_local.sh — Chạy smoke test 3 service trên localhost
# Dùng khi dev local, trước khi build Docker image
#
# Cách dùng:
#   1. Mở 3 terminal, mỗi terminal chạy 1 service:
#      cd services/product-service && pip install -r requirements.txt && python app.py
#      cd services/order-service   && pip install -r requirements.txt && python app.py
#      cd services/api-gateway     && ORDER_SERVICE_URL=http://localhost:8081 \
#                                      PRODUCT_SERVICE_URL=http://localhost:8082 \
#                                      python app.py
#   2. Terminal thứ 4: bash services/test_local.sh

set -euo pipefail

GW="http://localhost:8080"
OS="http://localhost:8081"
PS="http://localhost:8082"

ok()   { echo "  ✅ $1"; }
fail() { echo "  ❌ $1"; exit 1; }
sep()  { echo; echo "--- $1 ---"; }

sep "Health checks"
curl -sf "$GW/healthz" | grep -q "ok" && ok "api-gateway /healthz"     || fail "api-gateway"
curl -sf "$OS/healthz" | grep -q "ok" && ok "order-service /healthz"   || fail "order-service"
curl -sf "$PS/healthz" | grep -q "ok" && ok "product-service /healthz" || fail "product-service"

sep "Products"
curl -sf "$GW/products" | python3 -m json.tool | head -5
ok "GET /products"

curl -sf "$GW/products/1" | grep -q "Keyboard" && ok "GET /products/1" || fail "product detail"

sep "Orders"
curl -sf "$GW/orders" | python3 -m json.tool | head -5
ok "GET /orders"

# Tạo đơn hàng mới
RESPONSE=$(curl -sf -X POST "$GW/orders" \
  -H "Content-Type: application/json" \
  -d '{"product_id": 1, "quantity": 2}')
echo "$RESPONSE" | python3 -m json.tool
ORDER_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
ok "POST /orders → $ORDER_ID"

# Lấy đơn vừa tạo
curl -sf "$GW/orders/$ORDER_ID" | grep -q "$ORDER_ID" && ok "GET /orders/$ORDER_ID" || fail "get order"

sep "Stock check"
curl -sf "$PS/products/1/stock" | python3 -m json.tool
ok "GET /products/1/stock"

sep "Metrics endpoint (Prometheus scrape)"
curl -sf "$GW/metrics" | grep -q "flask_http_request_total" && ok "api-gateway /metrics" || fail "metrics"
curl -sf "$OS/metrics" | grep -q "flask_http_request_total" && ok "order-service /metrics" || fail "metrics"
curl -sf "$PS/metrics" | grep -q "product_reserve_total"    && ok "product-service custom metric" || fail "custom metric"

echo
echo "✅ Tất cả smoke test PASS"
