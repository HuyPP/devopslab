# Runbook: API Gateway High Error Rate

**Alert**: `ApiGatewayHighErrorBudgetBurn`  
**Severity**: Critical  
**SLO**: success_rate >= 99.5%

## Triệu chứng

Alert fire khi burn rate > 14.4 — error budget 30 ngày sẽ cạn trong ~2 ngày.

## Điều tra (5 phút đầu)

```bash
# 1. Xem pod nào đang lỗi
kubectl get pods -n demo -l app=api-gateway

# 2. Xem log pod lỗi gần nhất
kubectl logs -n demo -l app=api-gateway --tail=100 | grep -i error

# 3. Kiểm tra có canary đang chạy không
kubectl argo rollouts get rollout api-gateway -n demo

# 4. Query Prometheus — tỉ lệ lỗi theo pod
# flask_http_request_total{job="api-gateway", status=~"5..", namespace="demo"}
```

## Hành động

### Nếu canary đang chạy và là nguyên nhân

```bash
# Abort canary ngay — về stable version
kubectl argo rollouts abort api-gateway -n demo
```

### Nếu tất cả pod đều lỗi (không phải canary)

```bash
# Rollback qua Git (đúng cách GitOps)
git log --oneline -5          # xem history
git revert HEAD --no-edit     # revert commit cuối
git push                      # ArgoCD tự sync về version cũ
```

### Nếu upstream service (order/product) lỗi

```bash
kubectl logs -n demo -l app=order-service --tail=50
kubectl logs -n demo -l app=product-service --tail=50
```

## Verify đã fix

```bash
# Error rate phải về 0 sau khi fix
kubectl logs -n demo -l app=api-gateway --tail=20 | grep -c "500"
```
