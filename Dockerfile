# --- builder: install pinned deps + the package into an isolated venv -------
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml README.md ./
COPY nazoratchi ./nazoratchi
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

# --- runtime -----------------------------------------------------------------
FROM python:3.11-slim

# opencv-python-headless needs glib at runtime (no libgl1: headless build)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# v2 classifier baked in: immutable, checksum-verified at build, no runtime
# network dependency. This layer sits before the venv copy so source changes
# never re-download the 83MB model. Two URLs: some networks serve an HTML
# page on the plain release URL; the API asset endpoint is the fallback.
RUN mkdir -p /app/models && python - <<'EOF'
import hashlib, sys, urllib.request

EXPECTED = "e0746121167fd94c9b5327831472221dca88fb3746c2a5ffd6b6f301fc1ff04a"
URLS = [
    ("https://github.com/notAI-tech/NudeNet/releases/download/v0/classifier_model.onnx",
     {"User-Agent": "nazoratchi-build"}),
    ("https://api.github.com/repos/notAI-tech/NudeNet/releases/assets/31196404",
     {"User-Agent": "nazoratchi-build", "Accept": "application/octet-stream"}),
]
for url, headers in URLS:
    try:
        req = urllib.request.Request(url, headers=headers)
        data = urllib.request.urlopen(req, timeout=300).read()
    except Exception as e:
        print(f"fetch failed: {url}: {e}", file=sys.stderr)
        continue
    digest = hashlib.sha256(data).hexdigest()
    if digest == EXPECTED:
        open("/app/models/classifier_model.onnx", "wb").write(data)
        print(f"classifier model OK from {url}")
        sys.exit(0)
    print(f"checksum mismatch from {url}: {digest}", file=sys.stderr)
sys.exit("could not fetch a checksum-valid classifier model")
EOF

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

ENTRYPOINT ["nazoratchi", "--config", "config.yaml"]
