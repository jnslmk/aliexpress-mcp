FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    AE_REGION=DE \
    AE_CURRENCY=EUR \
    AE_LOCALE=de_DE

WORKDIR /app

# Chromium, for the product-detail browser transport (see browser.py). Search
# stays a plain HTTP call; only detail needs this, because AliExpress gates the
# MTop pdp endpoints against non-browser clients.
#
# Unlike the sibling baumarkt-mcp this runs **headless** and needs no Xvfb:
# headless Chromium was verified to get SUCCESS from pdp.pc.query, where those
# retailers' bot-walls specifically require a headed browser.
#
# Its own layer, depending on nothing that changes: a ~400 MB Chromium download
# must not be redone every time this project's code does.
RUN pip install "patchright>=1.49" \
    && patchright install --with-deps chromium

# Dependencies install from pyproject alone first so a code-only change does not
# bust the pip layer. Copy the package in afterwards.
COPY pyproject.toml README.md ./
COPY aliexpress_mcp ./aliexpress_mcp
RUN pip install .

# Key-less runtime — run as an unprivileged user regardless. Chromium lives in
# PLAYWRIGHT_BROWSERS_PATH, which root just wrote to, so hand it over too.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && chown -R mcp:mcp /opt/playwright
USER mcp

EXPOSE 8000

# start-period is generous: the first product lookup pays for a cold Chromium
# start, and a container still warming up must not be declared unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

CMD ["aliexpress-mcp"]
