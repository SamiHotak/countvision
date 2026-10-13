# CountVision cloud: API, Celery worker, beat and the video worker all use this one image.
# Build context = the repository root (it needs edge/ for the video analysis engine):
#   docker compose build api        (in cloud/)
# What goes into the build: Dockerfile.dockerignore next to this file (a short allow list).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    CV_DATA_DIR=/data \
    CV_VIDEO_MODEL=/opt/models/yolox_tiny.onnx \
    ORT_DISABLE_TELEMETRY=1

WORKDIR /app

# fonts-dejavu-core: PDF fallback font for other alphabets (Bitstream Vera licence).
# ffmpeg: annotated videos as H.264 (plays in browsers). It runs as a separate program (LGPL/GPL
#         binary from Debian, not linked into our code).
# libgl1, libglib2.0-0: needed by OpenCV (video analysis).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (cached layers), then the code. The pip download cache is a BuildKit cache
# mount: a build that fails half way (slow internet) keeps what it downloaded for the next try,
# and the cache never ends up inside the image.
COPY cloud/api/pyproject.toml ./cloud/
RUN --mount=type=cache,target=/root/.cache/pip mkdir cloud/countvision_cloud && echo '__version__ = "0"' > cloud/countvision_cloud/__init__.py \
    && pip install ./cloud && pip uninstall -y countvision-cloud && rm -rf cloud/countvision_cloud cloud/build

# The edge counting engine (same code as on the devices) + YOLOX-Tiny (Apache-2.0, 20 MB, checked).
COPY edge/pyproject.toml edge/README.md ./edge/
COPY edge/countvision_edge ./edge/countvision_edge
RUN --mount=type=cache,target=/root/.cache/pip pip install "./edge[onnx]" \
    && countvision-edge models download yolox_tiny --dir /opt/models

COPY cloud/api/countvision_cloud ./cloud/countvision_cloud
COPY cloud/api/alembic.ini ./cloud/
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps ./cloud \
    && useradd --uid 10002 --create-home countvision \
    && mkdir -p /data && chown countvision /data

USER countvision
WORKDIR /app/cloud
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# Wait for Postgres + Redis, update the schema, then serve.
CMD ["sh", "-c", "countvision-cloud wait && countvision-cloud migrate && exec uvicorn countvision_cloud.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers"]
