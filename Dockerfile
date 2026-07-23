FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AE_REGION=DE \
    AE_CURRENCY=EUR \
    AE_LOCALE=de_DE

WORKDIR /app

# Dependencies install from pyproject alone first so a code-only change does not
# bust the pip layer. Copy the package in afterwards.
COPY pyproject.toml README.md ./
COPY aliexpress_mcp ./aliexpress_mcp
RUN pip install .

# Key-less, read-only runtime — run as an unprivileged user regardless.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin mcp
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

CMD ["aliexpress-mcp"]
