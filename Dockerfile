# --- builder: install pinned deps + the package into an isolated venv -------
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml README.md ./
COPY gatekeeper ./gatekeeper
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

# --- runtime -----------------------------------------------------------------
FROM python:3.11-slim

# opencv-python-headless needs glib at runtime (no libgl1: headless build)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# v2 classifier baked in: immutable, checksum-verified at build, no runtime
# network dependency. This layer sits before the venv copy so source changes
# never re-download the 83MB model.
ADD --checksum=sha256:e0746121167fd94c9b5327831472221dca88fb3746c2a5ffd6b6f301fc1ff04a \
    https://github.com/notAI-tech/NudeNet/releases/download/v0/classifier_model.onnx \
    /app/models/classifier_model.onnx

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# build-time smoke test: system libs present, native wheels import
RUN python -c "import cv2, onnxruntime"

RUN useradd --uid 1000 --create-home app \
    && mkdir -p /app/data /app/logs \
    && chown -R app:app /app
USER app
WORKDIR /app
COPY --chown=app:app scripts ./scripts

ENTRYPOINT ["gatekeeper", "--config", "config.yaml"]
