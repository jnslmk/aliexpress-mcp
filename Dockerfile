FROM python:3.12-slim-bookworm AS obscura

# Official v0.2.6 render-enabled stealth release (commit 8534b401898ed17b62f69bdc3d5eb186bffb0d60).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && case "$(dpkg --print-architecture)" in \
         amd64) arch=x86_64; sha=ac8057374ec1b5cabb78ea2d08b2ed890d21d156a42b1ac23357fd049edc08c5 ;; \
         arm64) arch=aarch64; sha=403b4c8e3bcf777748a870fbb32c9281e997a6c30c5f8377c17305287fc3811f ;; \
         *) echo "Unsupported Obscura architecture" >&2; exit 1 ;; \
       esac \
    && curl --fail --location --proto '=https' --tlsv1.2 \
         "https://github.com/h4ckf0r0day/obscura/releases/download/v0.2.6/obscura-${arch}-linux-stealth.tar.gz" \
         --output /tmp/obscura.tar.gz \
    && echo "$sha  /tmp/obscura.tar.gz" | sha256sum --check --strict \
    && mkdir /opt/obscura \
    && tar -xzf /tmp/obscura.tar.gz -C /opt/obscura obscura obscura-worker \
    && chmod 755 /opt/obscura/obscura /opt/obscura/obscura-worker

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AE_REGION=DE \
    AE_CURRENCY=EUR \
    AE_LOCALE=de_DE

WORKDIR /app

# Obscura needs libstdc++; bookworm glibc 2.36 covers amd64 2.34 / arm64 2.35.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=obscura /opt/obscura /opt/obscura

COPY pyproject.toml README.md ./
COPY aliexpress_mcp ./aliexpress_mcp
RUN pip install .

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin mcp
USER mcp

# CDP is an internal loopback-only child service, never a published port.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

CMD ["aliexpress-mcp"]
