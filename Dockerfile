# Lean arm64 image for the Oracle VM: FastAPI + Playwright Chromium.
# Pomerado minting is omitted (no Node) to keep the image small.
# Requires BROWSER_PLATFORM_ACCESS_TOKEN when binding 0.0.0.0.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BROWSER_PLATFORM_DATA_DIR=/data \
    BROWSER_PLATFORM_HOST=0.0.0.0 \
    BROWSER_PLATFORM_PORT=8745 \
    BROWSER_PLATFORM_POMERADO_ENABLED=false \
    BROWSER_PLATFORM_CHROME_HEADLESS=true \
    BROWSER_PLATFORM_REQUIRE_TOKEN_ON_LOCALHOST=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 bp \
    && useradd --system --uid 10001 --gid bp --home-dir /app --shell /usr/sbin/nologin bp

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[browseruse]" \
    && python -m playwright install-deps chromium \
    && python -m playwright install chromium \
    && mkdir -p /data /ms-playwright \
    && chown -R bp:bp /app /data /ms-playwright \
    && rm -rf /root/.cache/pip /tmp/* /var/lib/apt/lists/*

VOLUME /data
EXPOSE 8745
USER bp

CMD ["python", "-m", "browser_platform", "--host", "0.0.0.0", "--port", "8745"]
