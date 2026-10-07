# CountVision edge agent - CPU image (Intel/AMD x86-64 and ARM64 mini-PCs).
# Runtime: ONNX Runtime (add OpenVINO with --build-arg EXTRAS=onnx,openvino). Default model: YOLOX-Tiny (Apache-2.0), baked into the image.
#
#   docker compose up -d --build        (from the docker/ folder, see docker/README.md)
#
# No AGPL code is installed in this image (no Ultralytics). Build with
#   --build-arg EXTRAS=onnx,yolo   if you need it (then the image contains AGPL-3.0 code).
ARG BASE=ubuntu:24.04
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    CV_DATA_DIR=/data \
    CV_MODELS=/opt/models \
    CV_CONFIG=/config/config.yaml \
    TZ=Europe/Berlin

# libgl1 + libglib2.0-0: needed by OpenCV. tzdata: local time for "today" in reports.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates tzdata libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv

WORKDIR /app
COPY edge/pyproject.toml edge/README.md /app/edge/
COPY edge/countvision_edge /app/edge/countvision_edge
ARG EXTRAS=onnx
RUN pip install "/app/edge[${EXTRAS}]" \
 && countvision-edge models download yolox_tiny --dir /opt/models

COPY edge/configs /app/configs
COPY docker/entrypoint.sh /usr/local/bin/countvision-entrypoint
RUN chmod 755 /usr/local/bin/countvision-entrypoint \
 && useradd --system --uid 10001 --gid 0 --groups video --create-home --home-dir /home/countvision countvision \
 && mkdir -p /data /config \
 && chown 10001:0 /data /config \
 && chmod 775 /data /config

# Runs as a normal user, never as root.
USER 10001
VOLUME ["/data", "/config"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=15s --start-period=120s --retries=3 \
  CMD ["countvision-entrypoint", "health"]
ENTRYPOINT ["countvision-entrypoint"]
CMD ["run"]
