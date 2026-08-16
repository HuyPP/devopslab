# DevOps Study Guide — Ôn tập từ đầu đến cuối
## Dựa trên project e-commerce devopslab thực tế

> Mỗi mục: **Khái niệm cốt lõi** → **Chỉ vào file thực tế** → **Câu hỏi kiểm tra** → **Thí nghiệm tay**

---

## Mục lục

1. [Docker — Image, Layer, Multi-stage](#1-docker)
2. [Kubernetes Objects — Pod, Deployment, Service](#2-kubernetes-objects)
3. [Config & Probe — ConfigMap, readiness, liveness](#3-config--probe)
4. [GitOps — ArgoCD, app-of-apps, sync-wave](#4-gitops--argocd)
5. [CI Pipeline — GitHub Actions, test, scan](#5-ci-pipeline--github-actions)
6. [Canary Deploy — Argo Rollouts, AnalysisTemplate](#6-canary-deploy)
7. [Observability — SLI/SLO, burn rate, alert](#7-observability)
8. [RBAC — Role, ClusterRole, Binding](#8-rbac)
9. [Admission Policy — Gatekeeper, Rego](#9-admission-policy--gatekeeper)
10. [Luồng end-to-end hoàn chỉnh](#10-luồng-end-to-end)
11. [Câu hỏi tổng hợp cuối khoá](#11-câu-hỏi-tổng-hợp)

---

## 1. Docker

### Khái niệm cốt lõi

```
Image  = blueprint read-only, gồm nhiều layer xếp chồng
Layer  = mỗi lệnh RUN/COPY/ADD trong Dockerfile tạo 1 layer
Cache  = Docker tái dùng layer nếu lệnh + context không đổi
Container = instance đang chạy từ image, có thêm writable layer
```

### File thực tế: `services/api-gateway/Dockerfile`

```dockerfile
# Stage 1 — builder: cài dependencies
FROM python:3.12.4-slim AS builder
WORKDIR /build
COPY requirements.txt .              # layer này cache khi chỉ sửa app.py
RUN pip install --prefix=/install -r requirements.txt

# Stage 2 — runtime: chỉ copy output, không copy pip/gcc
FROM python:3.12.4-slim AS runtime
RUN groupadd -r appgroup && useradd -r -g appgroup appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY app.py .
USER appuser                         # non-root
EXPOSE 8080
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "app:app"]
```

**Tại sao multi-stage?**
- Image cuối không chứa pip, gcc, build tools → nhỏ ~120MB thay vì ~400MB
- Ít package = ít CVE → Trivy scan ít báo lỗi hơn
- `USER appuser` đáp ứng Gatekeeper constraint F-04 (cấm root)

**Tại sao `COPY requirements.txt .` trước `COPY app.py .`?**
- Docker build layer theo thứ tự từ trên xuống
- Khi chỉ sửa `app.py`: layer `requirements.txt` + `pip install` đã cache → bỏ qua, chỉ re-run từ `COPY app.py` → build **nhanh hơn 80%**
- Đảo ngược: mỗi lần sửa code phải pip install lại từ đầu

**Tại sao gunicorn thay vì `flask run`?**
- `flask run` = single-threaded, debug mode, không production-safe
- `gunicorn --workers 2` = multi-process, graceful shutdown, proper signal handling

### Câu hỏi kiểm tra

1. `COPY --from=builder /install /usr/local` copy gì? Tại sao không copy toàn bộ `/build`?
2. Stage `builder` dùng `python:3.12.4-slim`, stage `runtime` cũng dùng `python:3.12.4-slim`. Nếu đổi runtime sang `python:3.12.4-alpine`, trade-off là gì?
3. `USER appuser` trong Dockerfile và `runAsUser: 1000` trong K8s deployment — cái nào có hiệu lực? Tại sao cần cả hai?

### Thí nghiệm

```bash
# Build và so sánh size
docker build -t api-gw:test services/api-gateway/
docker build --target builder -t api-gw:builder services/api-gateway/
docker images | grep api-gw
# api-gw:test    ~120MB   (chỉ runtime)
# api-gw:builder ~300MB   (có cả pip, build tools)

# Xem layers
docker history api-gw:test --no-trunc

# Test cache: sửa app.py rồi build lại → pip install không chạy lại
echo "# test" >> services/api-gateway/app.py
docker build -t api-gw:test2 services/api-gateway/
# Thấy: "CACHED" ở bước pip install
```

---

## 2. Kubernetes Objects

### Mental model: Desired State vs Actual State

```
Bạn khai báo: "tôi muốn 2 replica api-gateway luôn chạy"
K8s controller liên tục: observe → diff → reconcile
  Thực tế 1 pod → tạo thêm 1
  Thực tế 3 pod → xóa 1
Không bao giờ tắt — đây là control loop
```

### Phả hệ object

```
Deployment
  └─ ReplicaSet  (K8s tạo tự động, bạn không tạo trực tiếp)
       └─ Pod    (đơn vị nhỏ nhất — 1+ container share network/storage)
Service          (stable DNS + load balancer → tìm pod qua label selector)
ConfigMap        (config không nhạy cảm, inject qua env hoặc volume)
Namespace        (vách ngăn logic — tên object unique trong NS)
```

### File thực tế: `k8s/deployments.yaml` — api-gateway

```yaml
spec:
  replicas: 2                    # desired state
  selector:
    matchLabels:
      app: api-gateway           # label là "chất keo" kết nối Deployment→Pod
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1        # tạo 1 pod MỚI trước khi xóa pod cũ → tổng đỉnh = 3
      maxUnavailable: 0  # không để pod nào down → zero-downtime deploy
```

**Tại sao maxSurge:1 + maxUnavailable:0?**

Khi deploy version mới với replicas=2:
- K8s tạo pod mới (tổng = 3), đợi nó `Ready`
- Xóa 1 pod cũ (tổng = 2), tạo tiếp pod mới, đợi `Ready`
- Xóa pod cũ cuối (tổng = 2)
- Kết quả: **luôn có ≥ 2 pod sẵn sàng** trong suốt quá trình

### File thực tế: `k8s/services.yaml`

```yaml
# api-gateway: NodePort — expose ra máy host (dev/demo)
spec:
  type: NodePort
  selector:
    app: api-gateway    # tìm pod có label này
  ports:
    - port: 8080        # ClusterIP port (pod khác trong cụm dùng)
      targetPort: 8080  # port trên container
      nodePort: 30080   # port trên node (truy cập từ máy host)

# order-service: ClusterIP — chỉ trong cụm, api-gateway gọi
spec:
  type: ClusterIP       # mặc định
  # DNS tự động: http://order-service:8081
  #              http://order-service.demo.svc.cluster.local:8081
```

**Labels là gì và tại sao quan trọng?**

Labels = cặp key-value gắn lên object. Đây là cách K8s objects "tìm" nhau:
- `Service.selector: app=api-gateway` → route traffic vào pod có label đó
- `Deployment.selector.matchLabels` → biết pod nào thuộc về mình
- Không dùng IP (pod IP thay đổi liên tục sau restart)

### Câu hỏi kiểm tra

1. Xóa 1 pod thuộc Deployment bằng `kubectl delete pod` — điều gì xảy ra? Tại sao?
2. `kubectl delete pod` trên pod trần (không có Deployment) — điều gì xảy ra? Tại sao khác?
3. Nếu thay đổi `selector.matchLabels` của Service từ `app: api-gateway` sang `app: api-gw`, điều gì xảy ra với traffic?
4. `NodePort` vs `ClusterIP` vs `LoadBalancer` — khi nào dùng cái nào?

### Thí nghiệm

```bash
minikube start -p devopslab --driver=docker
kubectl apply -f k8s/

# Xem phả hệ
kubectl get deploy,rs,pods -n demo

# Self-healing: xóa pod → ReplicaSet tạo lại ngay
kubectl delete pod -l app=order-service -n demo
kubectl get pods -n demo -w   # watch: pod mới mọc lên trong vài giây

# Label experiment: tách pod ra khỏi Service
POD=$(kubectl get pod -l app=api-gateway -n demo -o name | head -1)
kubectl label $POD app=api-gateway-isolated --overwrite -n demo
kubectl get endpoints api-gateway -n demo   # pod này biến mất khỏi endpoint
# ReplicaSet thấy thiếu 1 pod → tạo pod mới
kubectl get pods -n demo -l app=api-gateway
```

---

## 3. Config & Probe

### ConfigMap — tách config khỏi image (12-factor app)

**File thực tế: `k8s/configmaps.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: api-gateway-config
  namespace: demo
  annotations:
    argocd.argoproj.io/sync-wave: "0"   # phải có TRƯỚC Deployment (wave 1)
data:
  ORDER_SERVICE_URL:   "http://order-service:8081"
  PRODUCT_SERVICE_URL: "http://product-service:8082"
  VERSION:             "v1"
  ERROR_RATE:          "0"    # đổi "0.3" → inject 30% lỗi để demo SLO
```

**File thực tế: `k8s/deployments.yaml`** — inject qua envFrom:

```yaml
envFrom:
  - configMapRef:
      name: api-gateway-config   # inject tất cả key-value thành env var
```

**File thực tế: `services/api-gateway/app.py`** — đọc trong code:

```python
ERROR_RATE = float(os.getenv("ERROR_RATE", "0"))  # default nếu không có env
VERSION    = os.getenv("VERSION", "v1")
ORDER_URL  = os.getenv("ORDER_SERVICE_URL", "http://order-service:8081")
```

**Tại sao wave 0 cho ConfigMap, wave 1 cho Deployment?**

Deployment dùng `envFrom: configMapRef` → khi pod start, K8s phải tìm ConfigMap đó. Nếu không có → pod ở trạng thái `CreateContainerConfigError`. Wave đảm bảo ConfigMap tồn tại trước.

### readinessProbe vs livenessProbe

```yaml
# readinessProbe: "Pod đã sẵn sàng nhận traffic chưa?"
# Fail → K8s bỏ pod này ra khỏi Service endpoint
# Dùng trong rolling update: đợi pod mới ready rồi mới xóa pod cũ
readinessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 5    # đợi 5s sau khi container start mới check lần đầu
  periodSeconds: 10         # check mỗi 10s
  failureThreshold: 3       # fail 3 lần liên tiếp → NotReady

# livenessProbe: "Pod còn sống không? Có bị deadlock/treo không?"
# Fail → K8s RESTART container
# Dùng để phát hiện app bị stuck (không crash nhưng không xử lý được request)
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 15   # đợi lâu hơn readiness (app cần warm up)
  periodSeconds: 20
  failureThreshold: 3       # fail 3 lần → restart container
```

**Kịch bản phân biệt:**
- App crash → container exit → K8s restart (không cần probe)
- App deadlock (goroutine leak, connection pool full) → không crash, nhưng mọi request timeout → **livenessProbe fail** → restart
- App đang warm up (loading model ML) → chưa sẵn sàng nhận traffic → **readinessProbe fail** → không route traffic vào, nhưng không restart

### Câu hỏi kiểm tra

1. `initialDelaySeconds: 15` trong livenessProbe, `initialDelaySeconds: 5` trong readinessProbe. Tại sao livenessProbe cần đợi lâu hơn?
2. Thay `ERROR_RATE: "0"` thành `ERROR_RATE: "0.3"` trong ConfigMap rồi apply — pod có tự restart không? Tại sao?
3. Nếu `/healthz` trong `services/api-gateway/app.py` cũng gọi `maybe_inject_error()`, điều gì xảy ra với pod khi `ERROR_RATE=1`?

### Thí nghiệm

```bash
# Inject lỗi qua ConfigMap (không cần build lại image)
kubectl patch configmap api-gateway-config -n demo \
  --patch '{"data":{"ERROR_RATE":"1"}}'
kubectl rollout restart deployment/api-gateway -n demo
kubectl get pods -n demo -w
# Pod mới lên → readinessProbe check /healthz → pass (healthz không inject lỗi)
# Nhưng / endpoint sẽ trả 500 100% → SLO bị break

# Restore
kubectl patch configmap api-gateway-config -n demo \
  --patch '{"data":{"ERROR_RATE":"0"}}'
kubectl rollout restart deployment/api-gateway -n demo

# Quan sát livenessProbe fail: sửa healthz trả 500
# (demo concept, đừng làm trên production)
kubectl exec -it deploy/api-gateway -n demo -- sh
# Không thể sửa file vì readOnlyRootFilesystem: true → đây là security feature!
```

---

## 4. GitOps & ArgoCD

### 4 nguyên tắc GitOps

| Nguyên tắc | Nghĩa | Trong project này |
|-----------|-------|-------------------|
| **Declarative** | Khai báo "muốn gì", không viết script "làm thế nào" | YAML files trong `k8s/` |
| **Versioned** | Mọi thay đổi lưu trong Git, có history | `git log` xem ai đổi gì lúc nào |
| **Pulled** | ArgoCD trong cụm tự kéo từ Git, không ai push vào | ArgoCD poll mỗi 3 phút |
| **Reconciled** | Liên tục so sánh Git vs cụm, lệch thì tự sửa | `selfHeal: true` trong syncPolicy |

### App-of-Apps pattern

**File thực tế: `argocd/root.yaml`**

```yaml
spec:
  source:
    path: argocd/apps        # scan thư mục này
  destination:
    namespace: argocd        # Application object sống ở đây
  syncPolicy:
    automated:
      prune: true            # xóa App con nếu file bị xóa khỏi Git
      selfHeal: true         # tự sửa nếu ai drift
```

**Luồng:**
```
kubectl apply -f argocd/root.yaml   ← CHỈ 1 LẦN DUY NHẤT

root Application
  → scan argocd/apps/
  → tạo: platform, kube-prometheus-stack, argo-rollouts,
          gatekeeper, monitoring-config, rbac, rollouts-config
  → mỗi App con tự sync resource của nó
  → platform App sync k8s/ → deploy 3 microservice
```

**Thêm service mới sau này:**
```bash
# Tạo file argocd/apps/new-service.yaml
# git push → root tự phát hiện → tạo Application → deploy
# KHÔNG kubectl apply gì thêm
```

### Sync-wave — đúng thứ tự

```
namespaces.yaml  wave: -1   Namespace demo phải có trước mọi thứ
configmaps.yaml  wave:  0   ConfigMap phải có trước Deployment
deployments.yaml wave:  1   Deployment dùng envFrom → cần ConfigMap
services.yaml    wave:  2   Service route traffic sau khi pod ready
```

Nếu không có wave: ArgoCD apply theo alphabet → `configmaps` trước `deployments` may mắn vì C < D, nhưng `namespaces` (n) sau `deployments` (d) → **lỗi namespace not found**. Wave là cách khai báo dependency tường minh.

### Rollback đúng cách

```bash
# ❌ SAI — bị ArgoCD self-heal ghi đè sau ~3 phút
kubectl rollout undo deployment/api-gateway -n demo

# ✅ ĐÚNG — thay đổi Git, ArgoCD tự sync về
git revert HEAD --no-edit
git push
# ArgoCD detect commit mới → sync → cụm về version trước
```

**Tại sao `kubectl rollout undo` thất bại?**

ArgoCD có `selfHeal: true`. Sau khi `kubectl rollout undo`, cụm diverge khỏi Git → ArgoCD thấy `OutOfSync` → apply lại Git version → cụm quay về version lỗi. Vòng lặp: bạn revert, ArgoCD revert lại bạn.

### Synced ≠ Healthy

- **Synced** = cụm khớp Git (YAML match) ✅
- **Healthy** = resource chạy đúng (pod Running, không crash) ✅
- **Trường hợp bẫy**: push image lỗi (v2 crash) → ArgoCD apply → `Synced ✅` nhưng pod `CrashLoopBackOff` → `Degraded ❌`

### Câu hỏi kiểm tra

1. `prune: true` trong root Application. Bạn xóa file `argocd/apps/platform.yaml` và git push. Điều gì xảy ra với namespace `demo` và các pod đang chạy?
2. `finalizers: resources-finalizer.argocd.argoproj.io` trong root.yaml. Nếu bỏ finalizer này, điều gì thay đổi khi bạn `kubectl delete application root -n argocd`?
3. Tại sao `gatekeeper.yaml` tách thành 2 Application (controller ở wave 0, policies ở wave 2)?

### Thí nghiệm

```bash
# Setup
kubectl create ns argocd
kubectl apply --server-side -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server

# Apply root — LẦN DUY NHẤT
kubectl apply -f argocd/root.yaml

# Quan sát App con tự tạo
kubectl -n argocd get applications -w

# Test self-heal
kubectl scale deploy/api-gateway -n demo --replicas=5
kubectl get deploy api-gateway -n demo -w   # ArgoCD kéo về 2 sau <3 phút

# Test rollback GitOps
# (sửa configmaps.yaml: ERROR_RATE "0" → "0.5", commit, push)
# (rồi: git revert HEAD --no-edit && git push)
# Quan sát ArgoCD sync về ERROR_RATE "0"
```

---

## 5. CI Pipeline — GitHub Actions

### Toàn bộ luồng CI/CD

```
Developer
  │ git push feature-branch
  ▼
Pull Request mở
  │
  └─► pr-checks.yml chạy TỰ ĐỘNG
        ├─ validate-manifests  → kubeconform check YAML syntax
        ├─ lint-and-test       → flake8 + pytest (3 service song song)
        └─ dockerfile-lint     → hadolint (anti-pattern check)
        [Cả 3 xanh → Merge button unlock]
        [Review + Approve → Merge]
  │
  ▼
merge vào main
  │
  └─► build-push.yml chạy TỰ ĐỘNG
        ├─ validate           → kubeconform lại (an toàn hơn)
        ├─ build  (×3)        → docker buildx, cache GHA
        ├─ scan   (×3)        → Trivy CVE HIGH/CRITICAL
        ├─ push   (×3)        → ghcr.io với tag: sha-abc1234
        └─ update-manifest    → sed image tag → git commit [skip ci]
                                 k8s/deployments.yaml
                                 k8s-rollouts/rollout-api-gateway.yaml
  │
  ▼
ArgoCD detect commit mới → sync → Rollout canary bắt đầu
```

**Nguyên tắc CI/CD tách bạch:**
- CI: validate + build + scan + push — **KHÔNG có kubectl, KHÔNG deploy**
- CD: ArgoCD đọc Git, tự deploy — **không phụ thuộc CI**

### pr-checks.yml — 3 jobs

```yaml
# Job 1: validate-manifests (tên này khớp Branch Protection setting)
- kubeconform -strict -ignore-missing-schemas k8s/ rbac/ argocd/
- grep "huypp" → fail nếu còn placeholder

# Job 2: lint-and-test (matrix: 3 service song song)
- flake8 app.py --max-line-length=120
- pytest tests/ -v --tb=short

# Job 3: dockerfile-lint
- hadolint Dockerfile (phát hiện anti-pattern: :latest, HEALTHCHECK missing)
```

### build-push.yml — 5 jobs

```yaml
validate → build (×3) → scan (×3) → push (×3) → update-manifest

# Job build: fail-fast: false
# → 1 service fail không block service khác → biết tất cả lỗi trong 1 run

# Job scan: 3 lần Trivy
# Lần 1: format table → đọc trong log
# Lần 2: format sarif → upload GitHub Security tab
# Lần 3: exit-code 1 → thật sự fail CI nếu có CVE

# Job update-manifest: sed + git commit [skip ci]
sed -i "s|image: ghcr.io/${OWNER}/api-gateway:.*|...:${SHORT_SHA}|g" \
  k8s/deployments.yaml
git commit -m "ci: update image tags to abc1234 [skip ci]"
git push
# [skip ci] → tránh trigger build-push.yml lại vô tận
```

### Tại sao tách pr-checks.yml và build-push.yml?

- PR check cần **nhanh** (~1 phút): chỉ validate + lint + test, không build image
- build-push cần **đầy đủ** (~8 phút): build + scan + push + update manifest
- Status check name phải stable: nếu dùng chung file với matrix, tên job thay đổi theo matrix value → Branch Protection không nhận ra

### Câu hỏi kiểm tra

1. `needs: [validate, scan]` trong job `push`. Nếu `scan` cho `order-service` fail nhưng `api-gateway` và `product-service` pass — với `fail-fast: false` — job `push` có chạy không?
2. Step `update-manifest` dùng `sed -i`. Vấn đề gì xảy ra nếu thiếu `[skip ci]` trong commit message?
3. `permissions: packages: write` chỉ khai báo trong job `push`, không khai báo ở level workflow. Tại sao? Nguyên tắc gì?
4. Job `scan` chạy Trivy **3 lần**. Tại sao không chạy 1 lần với `exit-code: 1`?

### Thí nghiệm

```bash
# Chạy test local trước khi push
cd services/api-gateway
pip install -r requirements.txt pytest==8.3.3 flake8==7.1.1
python -m pytest tests/ -v
flake8 app.py --max-line-length=120

# Tạo bad PR để xem CI block merge
git checkout -b test/bad-yaml
echo "bad: [unclosed" >> k8s/configmaps.yaml
git commit -am "test: bad yaml"
git push origin test/bad-yaml
# → Mở PR → validate-manifests ĐỎ → Merge bị lock

# Restore
git revert HEAD --no-edit
git push origin test/bad-yaml

# Xem GitHub Actions Security tab sau khi scan chạy
# GitHub → Security → Code scanning alerts → filter by tool: trivy
```

---

## 6. Canary Deploy

### Vấn đề với deploy "một phát 100%"

```
Deploy version lỗi → 100% user gặp lỗi ngay lập tức
Phát hiện muộn (khách than phiền sau 30 phút)
Rollback tay: người nhìn dashboard → quyết định → thực hiện → chậm
```

### Canary: thả traffic dần + tự chấm

**File thực tế: `k8s-rollouts/rollout-api-gateway.yaml`**

```yaml
strategy:
  canary:
    canaryService: api-gateway-canary    # nhận % canary
    stableService: api-gateway-stable    # nhận phần còn lại

    steps:
      - setWeight: 10      # 10% traffic → bản mới (1/4 pod nếu replicas=4)
      - analysis:          # chạy AnalysisRun kiểm tra metric ngay
          templates:
            - templateName: api-gateway-slo
      - setWeight: 25
      - pause: {duration: 2m}
      - setWeight: 50
      - pause: {duration: 2m}
      - setWeight: 100     # promote hoàn toàn
```

### AnalysisTemplate — "luật tự chấm"

**File thực tế: `k8s-rollouts/analysis-template.yaml`**

```yaml
metrics:
  - name: success-rate
    interval: 30s
    successCondition: result[0] >= 0.95    # PASS nếu >= 95% thành công
    failureCondition: result[0] < 0.90     # ABORT NGAY nếu < 90% (nghiêm trọng)
    failureLimit: 3                        # fail nhẹ (90-95%) → chịu 3 lần rồi mới abort
    provider:
      prometheus:
        query: |
          sum(rate(flask_http_request_total{status!~"5.."}[2m]))
          /
          sum(rate(flask_http_request_total[2m]))

  - name: latency-p95
    interval: 30s
    successCondition: result[0] < 0.5     # < 500ms → OK
    failureCondition: result[0] > 1.0     # > 1000ms → abort ngay
    failureLimit: 3
```

### Hai kịch bản

**Good run (bản mới tốt):**
```
10% → AnalysisRun: success_rate=0.99 ✅ → 25% → pause 2m
→ success_rate=0.98 ✅ → 50% → pause 2m → 100% → Healthy
Tổng: ~7 phút, 0% user bị ảnh hưởng
```

**Bad run (ERROR_RATE=0.3):**
```
10% → AnalysisRun: success_rate=0.70 < 0.90 → failureCondition triggered
→ fail lần 1 → 30s → fail lần 2 → 30s → fail lần 3 → ABORT
→ rollback về stable v1 tự động
Tổng: ~3 phút, chỉ 10% user từng gặp lỗi
```

### Câu hỏi kiểm tra

1. `canaryService` và `stableService` được dùng để làm gì? Argo Rollouts điều chỉnh % traffic bằng cơ chế nào khi không có Istio?
2. Tại sao `analysis` step đặt ngay sau `setWeight: 10` (step đầu tiên), không phải sau `setWeight: 50`?
3. `failureCondition: result[0] < 0.90` trigger abort ngay lập tức (không đợi `failureLimit: 3`). Tại sao threshold abort (90%) thấp hơn threshold fail (95%)?
4. Trong kịch bản bad run, **bao lâu** tối đa từ lúc deploy đến lúc rollback hoàn tất?

### Demo canary

```bash
# Bước 1: Inject lỗi vào bản mới
# Sửa k8s/configmaps.yaml: ERROR_RATE "0" → "0.3"
# Sửa k8s-rollouts/rollout-api-gateway.yaml: image tag v1 → v2
git commit -am "feat: v2 (30% error rate for demo)"
git push

# Bước 2: Tạo traffic
kubectl run load -n demo --image=busybox --restart=Never -- \
  sh -c "while true; do wget -qO- http://api-gateway:8080/; sleep 0.1; done"

# Bước 3: Theo dõi realtime
kubectl argo rollouts get rollout api-gateway -n demo --watch
# Quan sát: 10% → AnalysisRun Running → AnalysisRun Failed → Aborted → Stable

# Bước 4: Verify rollback về v1
kubectl get pods -n demo -l app=api-gateway \
  -o jsonpath='{range .items[*]}{.metadata.name} {.spec.containers[0].image}{"\n"}{end}'

# Bước 5: Dọn dẹp và restore
kubectl delete pod load -n demo
git revert HEAD --no-edit && git push
```

---

## 7. Observability

### 3 trụ cột

| Trụ cột | Trả lời | Công cụ | Đặc điểm |
|---------|---------|---------|----------|
| **Metrics** | CÓ vấn đề không? | Prometheus | Time-series, tổng hợp được, nhẹ |
| **Logs** | Lỗi CỤ THỂ là gì? | Loki | Chi tiết nhất, nặng |
| **Traces** | Chậm/lỗi ở BƯỚC nào? | Jaeger | Trace request qua nhiều service |

Kết nối qua `trace_id`: metric → log → trace = 3 cú click từ "có lỗi" đến "tắc ở DB 5s".

### SLA → SLO → SLI

```
SLA = hứa với khách (vi phạm → đền tiền)
SLO = mục tiêu nội bộ, chặt hơn SLA (biên an toàn)
SLI = số đo thực tế để biết có đạt SLO không

Project này:
  SLO: api-gateway success_rate >= 99.5% trong 30 ngày
  SLI: sum(rate(request_ok[1m])) / sum(rate(request_total[1m]))
  Error budget: 0.5% × 43,200 phút = 216 phút/tháng
```

### Burn rate — đang tiêu budget nhanh cỡ nào?

```
Burn rate = error_rate_hiện_tại / error_budget_rate (0.5%)

Ví dụ:
  Lỗi 0.5%  → burn rate = 1   → budget cạn sau 30 ngày  (bình thường)
  Lỗi 7.2%  → burn rate = 14.4 → budget cạn sau 2 ngày  (CRITICAL)
  Lỗi 3.0%  → burn rate = 6   → budget cạn sau 5 ngày   (WARNING)
```

### Multi-window alert

**File thực tế: `monitoring/slo-alerts.yaml`**

```yaml
# CRITICAL: Fast burn — cháy lớn, bắt ngay
# burn_rate[1h] > 14.4 AND burn_rate[5m] > 14.4
# Tại sao AND 2 window?
#   1h đơn lẻ: có thể flap (1 spike ngắn làm burn rate nhảy)
#   5m đơn lẻ: quá nhạy, false positive nhiều
#   AND cả 2: chắc chắn đang có vấn đề thật sự, không phải noise
- alert: ApiGatewayHighErrorBudgetBurn
  expr: |
    (error_rate[1h] / 0.005 > 14.4)
    AND
    (error_rate[5m] / 0.005 > 14.4)
  for: 2m    # duy trì 2 phút mới alert → giảm flapping

# WARNING: Slow burn — rò rỉ âm ỉ
# burn_rate[6h] > 6 AND burn_rate[30m] > 6
- alert: ApiGatewayMediumErrorBudgetBurn
  for: 15m
```

### Recording rules — tại sao cần?

```yaml
# Thay vì alert query thẳng vào raw metric mỗi 30s:
sum(rate(flask_http_request_total{...}[1m])) / sum(rate(...))

# Dùng recording rule: pre-compute mỗi 1 phút, lưu vào time-series mới
- record: job:flask_http_success_rate:rate1m
  expr: sum(rate(...)) / sum(rate(...))

# Alert query dùng recording rule: nhanh hơn, ít tải Prometheus
```

Production với triệu request/giây: raw metric query mỗi 30s × số alert = Prometheus quá tải. Recording rule compute 1 lần, nhiều alert dùng chung.

### Câu hỏi kiểm tra

1. Tính error budget: SLO = 99.9% trong 30 ngày. Error budget là bao nhiêu phút?
2. Nếu `error_rate = 3%`, burn rate là bao nhiêu (với SLO 99.5%)? Budget cạn sau bao nhiêu ngày?
3. `for: 2m` trong alert CRITICAL. Nếu bỏ `for`, điều gì xảy ra khi có 1 spike lỗi ngắn 30 giây?
4. Tại sao `SLI phải đo từ góc người dùng` (success rate của request), không phải CPU < 80%?

### Thí nghiệm

```bash
# Port-forward Prometheus
kubectl -n monitoring port-forward \
  svc/kube-prometheus-stack-prometheus 9090 &

# Mở http://localhost:9090 → Graph, thử từng query:

# 1. Success rate hiện tại
sum(rate(flask_http_request_total{namespace="demo",status!~"5.."}[2m]))
/ sum(rate(flask_http_request_total{namespace="demo"}[2m]))

# 2. Inject lỗi (30%) và watch burn rate
kubectl patch configmap api-gateway-config -n demo \
  --patch '{"data":{"ERROR_RATE":"0.3"}}'
kubectl rollout restart deploy/api-gateway -n demo

# Load test
kubectl run load -n demo --image=busybox --restart=Never -- \
  sh -c "while true; do wget -qO- http://api-gateway:8080/ 2>/dev/null; done"

# Query burn rate (update mỗi 30s)
(sum(rate(flask_http_request_total{namespace="demo",status=~"5.."}[5m]))
 / sum(rate(flask_http_request_total{namespace="demo"}[5m]))) / 0.005
# Thấy: ~60 (0.3/0.005 = 60) → cháy cực nhanh

# Xem recording rule đã được compute chưa
job:flask_http_success_rate:rate1m

# Restore
kubectl patch configmap api-gateway-config -n demo \
  --patch '{"data":{"ERROR_RATE":"0"}}'
kubectl delete pod load -n demo
```

---

## 8. RBAC

### 4 thứ cần nhớ: 2 cặp

```
Role          = định nghĩa quyền trong 1 namespace
ClusterRole   = định nghĩa quyền toàn cụm

RoleBinding        = gắn Role/ClusterRole cho user trong 1 namespace
ClusterRoleBinding = gắn ClusterRole cho user toàn cụm
```

**Quan trọng:** Role chưa gắn = chưa có tác dụng gì.

### 3 vai trò trong project

**File thực tế: `rbac/roles.yaml` + `rbac/rolebindings.yaml`**

```
alice → developer (Role, ns demo)
  - CRUD deployments/pods/services/configmaps trong ns demo
  - KHÔNG có: secrets (sensitive), rolebindings (leo thang quyền)
  - Dùng RoleBinding → chỉ ns demo, không với sang kube-system

bob → sre (ClusterRole + ClusterRoleBinding)
  - get/list/watch/delete pods toàn cụm
  - update/patch deployments (scale, rollout)
  - get nodes, events, metrics

carol → viewer (ClusterRole + ClusterRoleBinding)
  - CHỈ get/list/watch — không create/update/delete
  - Không có nodes, secrets (sensitive)
```

### Tại sao alice dùng Role, không phải ClusterRole?

```yaml
# alice → RoleBinding (namespace-scoped)
kind: RoleBinding
metadata:
  namespace: demo          # ràng buộc trong ns demo
subjects:
  - kind: User
    name: alice
roleRef:
  kind: Role
  name: developer          # Role cũng ở ns demo
```

Nếu dùng `ClusterRoleBinding` với `ClusterRole developer` → alice có quyền CRUD deployments trong **mọi namespace** kể cả `kube-system`, `argocd` — phá vỡ nguyên tắc least-privilege.

### Test với kubectl auth can-i

```bash
kubectl auth can-i create deploy -n demo --as alice         # yes
kubectl auth can-i create deploy -n kube-system --as alice  # no ← quan trọng
kubectl auth can-i get pods -A --as bob                     # yes
kubectl auth can-i delete nodes --as carol                  # no
kubectl auth can-i delete nodes --as bob                    # no (SRE không xóa node)
kubectl auth can-i get secrets -n demo --as alice           # no (developer không đọc secret)
```

### Câu hỏi kiểm tra

1. alice cần đọc DB password để debug. Role `developer` không có `secrets`. Cách đúng để xử lý là gì?
2. Nếu dùng `ClusterRoleBinding` cho alice thay vì `RoleBinding`, test case nào trong 6 câu trên sẽ thay đổi kết quả?
3. `subjects.kind: User` — trong K8s, "User" này được authenticate ở đâu? Có object `User` trong K8s không?

### Thí nghiệm

```bash
kubectl apply -f rbac/

# Chạy cả 6 test cases
kubectl auth can-i create deploy -n demo --as alice
kubectl auth can-i create deploy -n kube-system --as alice
kubectl auth can-i get pods -A --as bob
kubectl auth can-i delete nodes --as carol
kubectl auth can-i delete nodes --as bob
kubectl auth can-i get secrets -n demo --as alice

# Impersonation thật: hoạt động như alice
kubectl get pods -n demo --as alice              # OK
kubectl get pods -n kube-system --as alice       # Forbidden
kubectl delete deploy api-gateway -n demo --as alice  # OK (developer có quyền)
kubectl delete deploy api-gateway -n demo --as carol  # Forbidden (viewer)
```

---

## 9. Admission Policy — Gatekeeper

### RBAC vs Admission Policy

```
RBAC          = kiểm tra "AI làm gì" (authentication + authorization)
Admission     = kiểm tra "manifest như thế nào" (validation)

Luồng request:
  kubectl apply → [1] Authn → [2] Authz (RBAC) → [3] Admission → etcd

Alice có quyền create Deployment (RBAC pass)
  nhưng Deployment dùng image :latest → Gatekeeper REJECT
→ Cần cả 2 lớp mới đủ bảo vệ
```

### ConstraintTemplate + Constraint

```
ConstraintTemplate = khuôn mẫu (viết Rego logic 1 lần, sinh ra CRD mới)
Constraint         = instance của template (truyền param, chọn scope, enforce/audit)
```

**Ví dụ: require-owner-label**

```yaml
# ConstraintTemplate: gatekeeper/templates/require-owner-label.yaml
rego: |
  violation[{"msg": msg}] {
    provided := {label | input.review.object.metadata.labels[label]}
    required  := {label | label := input.parameters.labels[_]}
    missing   := required - provided    # set subtraction
    count(missing) > 0
    msg := sprintf("Thiếu label: %v", [missing])
  }

# Constraint: gatekeeper/constraints/constraints.yaml
kind: K8sRequiredLabels    # CRD sinh ra từ template
spec:
  enforcementAction: deny
  match:
    kinds: [{apiGroups: ["apps"], kinds: ["Deployment"]}]
    namespaces: ["demo"]
  parameters:
    labels: ["owner"]       # truyền tham số vào Rego
```

### 5 constraints trong project

| Constraint | Lý do bảo mật |
|-----------|---------------|
| no-latest-tag | `:latest` không immutable → không biết đang chạy version nào |
| require-resource-limits | Không có limit → pod ăn hết RAM node → evict 20 pod khác |
| no-root-user | Container root = nếu escape, attacker có root trên node |
| no-host-network | `hostNetwork=true` → pod thấy traffic trên node → sniff credential |
| require-owner-label | Ai chịu trách nhiệm resource? Alert routing, cost allocation |

### Sync-wave cho Gatekeeper

```
wave 0: gatekeeper controller (CRD phải có trước)
wave 1: ConstraintTemplate     (sinh ra CRD K8sNoLatestTag, v.v.)
wave 2: Constraint             (dùng CRD vừa tạo)

Nếu Constraint apply trước ConstraintTemplate:
  "no matches for kind K8sNoLatestTag" → Error
```

### Workflow bật Gatekeeper an toàn

```bash
# 1. Bật warn trước (audit mode)
# Sửa constraints.yaml: enforcementAction: deny → warn

# 2. Xem violations hiện tại
kubectl get k8snolatesttag.constraints.gatekeeper.sh \
  require-owner-label -o yaml | grep -A10 violations

# 3. Sửa manifest vi phạm (platform phải hợp lệ trước khi enforce)

# 4. Bật deny
# Sửa constraints.yaml: enforcementAction: warn → deny
git commit -am "security: enable gatekeeper enforce mode"
git push
```

### Câu hỏi kiểm tra

1. `excludedNamespaces: ["kube-system", "argocd", "gatekeeper-system"]` — tại sao phải exclude các namespace này?
2. Deployment của project (`k8s/deployments.yaml`) có pass tất cả 5 constraints không? Kiểm tra từng điều kiện.
3. Viết Rego rule kiểm tra: Deployment không được có `replicas > 5`.

### Thí nghiệm

```bash
# Apply templates trước, constraints sau
kubectl apply -f gatekeeper/templates/
kubectl apply -f gatekeeper/constraints/

# Test bad pods (phải bị reject)
kubectl apply -f gatekeeper/test/bad-pods.yaml
# Thấy: admission webhook denied the request: ...

# Test good pod (phải pass)
kubectl apply -f gatekeeper/test/good-pod.yaml
kubectl delete pod good-pod -n demo

# Tự tạo violation để test
kubectl apply -n demo -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bad-deploy
  namespace: demo
  # Thiếu label "owner" → phải bị reject
spec:
  replicas: 1
  selector:
    matchLabels: {app: bad}
  template:
    metadata:
      labels: {app: bad}
    spec:
      securityContext:
        runAsUser: 1000
      containers:
      - name: app
        image: nginx:1.27.0
        resources:
          limits: {cpu: 100m, memory: 64Mi}
EOF
# Error: Thiếu label: {"owner"}
```

---

## 10. Luồng end-to-end hoàn chỉnh

### Setup từ đầu (1 lần)

```bash
# 1. Thay username
$u = "your-github-username"
Get-ChildItem -Recurse -Filter "*.yaml" | ForEach-Object {
  (Get-Content $_.FullName -Raw) -replace 'huypp', $u |
  Set-Content $_.FullName -NoNewline
}
git add . && git commit -m "chore: replace placeholders" && git push

# 2. Dựng cụm
minikube start -p devopslab --driver=docker --cpus=4 --memory=6g

# 3. Build và load image vào minikube (không cần registry)
docker build -t ghcr.io/$u/api-gateway:v1 services/api-gateway/
docker build -t ghcr.io/$u/order-service:v1 services/order-service/
docker build -t ghcr.io/$u/product-service:v1 services/product-service/
minikube image load ghcr.io/$u/api-gateway:v1 -p devopslab
minikube image load ghcr.io/$u/order-service:v1 -p devopslab
minikube image load ghcr.io/$u/product-service:v1 -p devopslab

# 4. Cài ArgoCD
kubectl create ns argocd
kubectl apply --server-side -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server

# 5. Apply root — LẦN DUY NHẤT
kubectl apply -f argocd/root.yaml

# 6. Theo dõi
kubectl -n argocd get applications -w
# platform → Synced/Healthy
# kube-prometheus-stack → Synced/Healthy (chờ ~5 phút)
# argo-rollouts → Synced/Healthy
```

### Verify hệ thống hoạt động

```bash
# Xem tất cả pod
kubectl get pods -n demo
kubectl get pods -n monitoring
kubectl get pods -n argo-rollouts

# Test API
GW=$(minikube service api-gateway -n demo --url -p devopslab)
curl $GW/products
curl $GW/orders
curl -X POST $GW/orders \
  -H 'Content-Type: application/json' \
  -d '{"product_id":1,"quantity":2}'

# Xem Grafana
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80 &
# http://localhost:3000  admin/devopslab123

# Xem ArgoCD UI
kubectl -n argocd port-forward svc/argocd-server 8080:443 &
# https://localhost:8080  admin/<password từ secret>
PASS=$(kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d)
echo $PASS
```

### Scenario 1: Normal deploy (qua CI/CD)

```bash
# 1. Tạo feature branch
git checkout -b feat/add-discount

# 2. Sửa code (vd: thêm field discount vào product-service)

# 3. Chạy test local
cd services/product-service
python -m pytest tests/ -v

# 4. Push và mở PR
git push origin feat/add-discount
# → pr-checks.yml chạy → validate + lint + test

# 5. Merge sau khi CI xanh
# → build-push.yml chạy
# → Trivy scan → push image → update manifest
# → git commit "ci: update image tags to abc1234 [skip ci]"

# 6. ArgoCD detect → sync → Rollout canary
kubectl argo rollouts get rollout api-gateway -n demo --watch
```

### Scenario 2: Canary auto-abort (bản lỗi)

```bash
# 1. Inject lỗi
git checkout -b test/buggy-release
# Sửa k8s/configmaps.yaml: ERROR_RATE "0" → "0.3"
git commit -am "feat: new feature (có bug ẩn)"
git push && # mở PR và merge

# 2. Tạo traffic
kubectl run load -n demo --image=busybox --restart=Never -- \
  sh -c "while true; do wget -qO- http://api-gateway:8080/; sleep 0.1; done"

# 3. Theo dõi
kubectl argo rollouts get rollout api-gateway -n demo --watch
# 10% → AnalysisRun Failed → Aborted → Stable (< 3 phút)

# 4. Rollback manifest
kubectl delete pod load -n demo
git revert HEAD --no-edit && git push
```

### Scenario 3: Test RBAC + Gatekeeper cùng lúc

```bash
kubectl apply -f rbac/
kubectl apply -f gatekeeper/templates/
kubectl apply -f gatekeeper/constraints/

# Alice thử deploy manifest hợp lệ → OK
kubectl apply -n demo --as alice -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: valid-deploy
  namespace: demo
  labels:
    app: valid
    owner: alice-team    # có owner label → Gatekeeper pass
spec:
  replicas: 1
  selector:
    matchLabels: {app: valid}
  template:
    metadata:
      labels: {app: valid}
    spec:
      securityContext:
        runAsUser: 1000
      containers:
      - name: app
        image: nginx:1.27.0    # pin version → Gatekeeper pass
        resources:
          limits: {cpu: 100m, memory: 64Mi}  # có limits → Gatekeeper pass
EOF

# Alice thử deploy manifest thiếu owner label → Gatekeeper REJECT
# (kể cả khi RBAC cho phép alice create deploy)
kubectl apply -n demo --as alice -f gatekeeper/test/bad-pods.yaml
# Error: admission webhook denied...

# Carol thử bất cứ thứ gì → RBAC REJECT trước khi đến Gatekeeper
kubectl apply -n demo --as carol -f gatekeeper/test/good-pod.yaml
# Error: forbidden (carol chỉ có get/list/watch)
```

---

## 11. Câu hỏi tổng hợp cuối khoá

Trả lời không xem tài liệu. Mỗi câu trỏ về 1 file thực tế trong project.

---

### Nhóm Docker

**D1.** Trong `Dockerfile` của api-gateway, tại sao `COPY requirements.txt .` nằm ở dòng 19 nhưng `COPY app.py .` ở dòng 27? Khi bạn chỉ sửa 1 dòng trong `app.py` và build lại, Docker thực hiện bao nhiêu layer từ cache?

**D2.** Image dùng `USER appuser` (non-root). Nhưng trong `k8s/deployments.yaml` vẫn có `securityContext.runAsUser: 1000`. Giải thích tại sao cần cả hai, và trường hợp nào chỉ có Dockerfile mà không có K8s securityContext vẫn bị root?

**D3.** `CMD ["python", "-m", "gunicorn", "--workers", "2", "app:app"]`. Nếu container nhận `SIGTERM` (K8s graceful shutdown), gunicorn xử lý thế nào? Flask dev server xử lý khác gì?

---

### Nhóm Kubernetes

**K1.** Mở file `k8s/deployments.yaml`. Rolling update với `maxSurge:1, maxUnavailable:0, replicas:2`. Bạn push image mới → Deployment bắt đầu update. Vẽ sơ đồ trạng thái pod ở mỗi bước.

**K2.** `readinessProbe.failureThreshold: 3` và `periodSeconds: 10`. Pod mới start lúc t=0, nhưng app cần 25 giây warm up. `initialDelaySeconds: 5`. Tại thời điểm nào pod mới được đưa vào Service endpoint?

**K3.** Trong `k8s/services.yaml`, `order-service` dùng `ClusterIP`. Từ pod `api-gateway`, làm thế nào tìm được địa chỉ của `order-service`? (Gợi ý: xem `app.py` dòng `ORDER_URL`). Điều gì xảy ra nếu `order-service` pod chết và được tạo lại với IP mới?

**K4.** `readOnlyRootFilesystem: true` trong securityContext. Trong thí nghiệm bạn thử `kubectl exec -it deploy/api-gateway -- sh` và `touch /tmp/test`. Kết quả là gì? Tại sao đây là security feature?

---

### Nhóm GitOps

**G1.** `argocd/root.yaml` có `prune: true`. Bạn xóa `argocd/apps/platform.yaml` và `git push`. Liệt kê chính xác những gì bị xóa (theo thứ tự).

**G2.** Developer A `kubectl scale deploy/api-gateway -n demo --replicas=5` để xử lý traffic spike. 3 phút sau, replicas về 2. Giải thích cơ chế. Developer A nên làm gì đúng cách?

**G3.** Phân biệt `Synced` và `Healthy` trong ArgoCD. Cho ví dụ tình huống `Synced=True, Healthy=False` xảy ra trong project này.

---

### Nhóm CI/CD

**C1.** `build-push.yml` job `update-manifest` dùng `sed -i` thay image tag rồi `git commit [skip ci]`. Nếu bỏ `[skip ci]`, mô tả vòng lặp vô tận sẽ xảy ra.

**C2.** Job `scan` có `fail-fast: false` trong matrix strategy. Job `push` có `needs: [validate, scan]`. Nếu scan của `order-service` fail nhưng `api-gateway` và `product-service` pass — image nào được push lên ghcr.io?

**C3.** `pr-checks.yml` check `grep "huypp"`. Loại kiểm tra này thuộc nhóm nào trong CI best practices? Tại sao đặt ở PR stage thay vì build stage?

---

### Nhóm Canary

**CA1.** `analysis-template.yaml`: `successCondition: result[0] >= 0.95` và `failureCondition: result[0] < 0.90`. Nếu success rate = 0.92 (giữa 2 ngưỡng), AnalysisRun làm gì?

**CA2.** Trong kịch bản bad run với `ERROR_RATE=0.3`, tính thời gian tối đa từ lúc deploy đến lúc rollback hoàn tất: `interval=30s`, `failureCondition` trigger khi `< 0.90`, `failureLimit=3`.

**CA3.** Tại sao `analysis` step đặt ngay sau `setWeight: 10` (không phải sau `50%` hay sau `100%`)? Trade-off là gì nếu đặt muộn hơn?

---

### Nhóm Observability

**O1.** SLO = 99.5%, khoảng thời gian 30 ngày. Tính: error budget (phút), burn rate khi error_rate=7.2%, số ngày budget cạn.

**O2.** Alert `ApiGatewayHighErrorBudgetBurn` có 2 điều kiện AND: `[1h] > 14.4 AND [5m] > 14.4`. Cho ví dụ tình huống mà chỉ `[5m]` thỏa mãn nhưng `[1h]` không — và tình huống ngược lại. Tại sao cả 2 trường hợp không nên alert?

**O3.** Trong `analysis-template.yaml`, query Prometheus dùng `flask_http_request_total` thay vì đọc log trực tiếp. Tại sao không đọc log để đếm lỗi 500?

---

### Nhóm Security

**S1.** `rbac/roles.yaml`: Role `developer` cho alice không có `secrets` trong resources. Alice cần biết DB_PASSWORD để debug. Quy trình đúng là gì?

**S2.** `gatekeeper/constraints/constraints.yaml`: constraint `require-resource-limits` exclude namespace `monitoring` và `argo-rollouts`. Tại sao? Nếu không exclude, vấn đề gì xảy ra?

**S3.** Vẽ sơ đồ luồng khi alice chạy `kubectl apply -f deploy-no-owner.yaml -n demo`. Đánh dấu rõ bước nào RBAC kiểm tra, bước nào Gatekeeper kiểm tra.

---

### Câu hỏi tích hợp (khó nhất)

**I1.** Mô tả toàn bộ luồng từ `git push` của developer đến pod mới chạy trong cụm. Bao gồm: GitHub Actions jobs, ArgoCD steps, K8s controller actions. Trỏ đến file cụ thể ở mỗi bước.

**I2.** `ERROR_RATE` được dùng trong 5 layer khác nhau: app code, ConfigMap, canary demo, SLO alert, AnalysisTemplate. Giải thích vai trò của nó ở mỗi layer và tại sao đây là thiết kế tốt.

**I3.** Nếu phải thêm `notification-service` mới vào hệ thống (cùng pattern), liệt kê chính xác các file cần tạo/sửa và thứ tự thực hiện.

---

## Tóm tắt các số quan trọng cần thuộc

```
Hệ thống:
  api-gateway:     port 8080, NodePort 30080
  order-service:   port 8081, ClusterIP
  product-service: port 8082, ClusterIP

Resources:
  requests: cpu=50m, memory=64Mi
  limits:   cpu=200m, memory=128Mi

Probes:
  readiness: initialDelay=5s, period=10s, failureThreshold=3
  liveness:  initialDelay=15s, period=20s, failureThreshold=3

SLO:
  success_rate >= 99.5%
  error_budget = 216 phút/tháng
  burn_rate_critical = 14.4 (2 ngày)
  burn_rate_warning  = 6    (5 ngày)

AnalysisTemplate:
  interval = 30s
  success: >= 0.95
  fail soft: < 0.90 (đợi 3 lần)
  latency: p95 < 500ms

Canary steps: 10% → 25% (pause 2m) → 50% (pause 2m) → 100%

Sync-waves: namespace(-1) → configmap(0) → deployment(1) → service(2)
            gatekeeper-controller(0) → template(1) → constraint(2)

RBAC:
  alice: Role (demo ns) — developer
  bob:   ClusterRole — sre
  carol: ClusterRole — viewer
```

---

*Hoàn thành tất cả thí nghiệm trong file này = sẵn sàng cho capstone project.*
