# =============================================================================
# api-gateway/Dockerfile
# =============================================================================
# Multi-stage build:
#   Stage 1 (builder) — cài dependencies vào virtual env
#   Stage 2 (runtime) — copy venv, KHÔNG copy source build tools
#
# Tại sao multi-stage?
#   - Image cuối không chứa pip, gcc, build headers → nhỏ hơn ~200MB
#   - Giảm attack surface (ít package = ít CVE)
#   - Gatekeeper constraint "image phải pin version" → dùng python:3.12.4-slim cụ thể
# =============================================================================

# --- Stage 1: builder ---
FROM python:3.12.4-slim AS builder

WORKDIR /build

# Copy requirements trước — Docker cache layer này khi code thay đổi mà
# requirements không đổi thì không cần pip install lại → build nhanh hơn
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Stage 2: runtime ---
FROM python:3.12.4-slim AS runtime

# Tạo non-root user — đáp ứng Gatekeeper rule: runAsUser != 0
# (Gatekeeper constraint F-04 trong W10 sẽ reject pod chạy root)
RUN groupadd -r appgroup && useradd -r -g appgroup appuser

WORKDIR /app

# Copy chỉ venv đã build từ stage 1
COPY --from=builder /install /usr/local

# Copy application source
COPY app.py .

# Chạy với non-root user
USER appuser

EXPOSE 8080

# Dùng gunicorn thay vì flask dev server:
#   - flask dev server KHÔNG production-safe (single-threaded, debug mode)
#   - gunicorn: multi-worker, graceful shutdown, proper signal handling
# Số worker = 2*CPU + 1 là rule-of-thumb cho I/O-bound app
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "30", "app:app"]
