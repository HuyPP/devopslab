# =============================================================================
# product-service/Dockerfile
# =============================================================================

FROM python:3.12.4-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12.4-slim AS runtime
RUN groupadd -r appgroup && useradd -r -g appgroup appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY app.py .
USER appuser
EXPOSE 8082
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8082", "--workers", "2", "--timeout", "30", "app:app"]
