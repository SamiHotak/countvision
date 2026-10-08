# CountVision cloud API (also used for the Celery worker and beat).
# Build from the cloud/ folder:  docker compose build api
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first (cached layer), then the code.
COPY pyproject.toml ./
RUN mkdir countvision_cloud && echo '__version__ = "0"' > countvision_cloud/__init__.py \
    && pip install . && pip uninstall -y countvision-cloud && rm -rf countvision_cloud build

COPY countvision_cloud ./countvision_cloud
COPY alembic.ini ./
RUN pip install --no-deps . \
    && useradd --uid 10002 --create-home countvision

USER countvision
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# Wait for Postgres + Redis, update the schema, then serve.
CMD ["sh", "-c", "countvision-cloud wait && countvision-cloud migrate && exec uvicorn countvision_cloud.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers"]
