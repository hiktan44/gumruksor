# Use Python 3.12 slim image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies for Playwright and Python packages.
# `apt-get upgrade` applies Debian security fixes on top of the base image so
# the container scan (Trivy, CRITICAL/HIGH with fixes) stays green when the
# upstream image lags behind Debian's security archive.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && apt-get install -y \
    gcc \
    g++ \
    antiword \
    catdoc \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Copy Python files and the reviewed dependency lock
COPY pyproject.toml uv.lock setup.py README.md ./
COPY *.py ./
COPY ticaret_sources.json ./
COPY customs_sources.json ./
COPY tariff_sources.json ./
COPY control_sources.json ./
COPY data/ ./data/
COPY semantic_search/ ./semantic_search/
COPY benchmarks/ ./benchmarks/
COPY web/ ./web/

# Install the exact audited dependency set into the project virtual environment
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install Playwright browsers (Chromium only for smaller image)
RUN playwright install --with-deps chromium

# Run the public web service without root privileges
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /ms-playwright /data
USER appuser

# Expose port
EXPOSE 8000

# Set environment variables
ENV PORT=8000
ENV PYTHONUNBUFFERED=1
ENV CONTAINER_ENV=1
ENV MEVZUAT_DATA_DIR=/data
ENV HOME=/home/appuser
VOLUME ["/data"]

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=5)" || exit 1

# Run the ASGI application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Agentic Security Firewall: Katman 2 (non-root hardening) ---
RUN (id -u appuser >/dev/null 2>&1 || useradd -m -u 10001 appuser) && { [ ! -d /app ] || chown -R appuser:appuser /app; }
USER appuser
