# AliExpress MCP

A self-hosted [MCP](https://modelcontextprotocol.io) server that lets an LLM
**search AliExpress and inspect product listings** — with **no API key**.

AliExpress has no official public product-search API, so this server mirrors what
the aliexpress.com web frontend does:

- **Search** — fetches the server-rendered search page
  (`/w/wholesale-<query>.html`) and pulls the product list out of the
  `_init_data_` JSON the page embeds for its own hydration. On an anti-bot
  (TMD) punish page it falls back to loading the same URL in Obscura.
- **Product detail** — tries AliExpress's internal **MTop** API
  (`acs.aliexpress.com`) first. When gated, Obscura loads the market's product
  page and calls its native MTop SDK with the observed `api`, `v`, `appKey`
  and `data`, requesting credentialed JSON instead of automatic JSONP.
  The provider SDK retains signing and token refresh. See
  [how product detail is fetched](#how-product-detail-is-fetched-and-why-a-browser).

TLS fingerprinting via [`curl_cffi`](https://github.com/lexiforest/curl_cffi)
(Chrome impersonation) is usually enough for search, which tries a plain HTTP
call first. Browser fallback uses ordinary
[Playwright](https://playwright.dev/python/) over a private loopback CDP service
owned by the serial worker, running
[Obscura v0.2.6](https://github.com/h4ckf0r0day/obscura/releases/tag/v0.2.6)
with rendering and stealth enabled. No Chromium download, Xvfb or public CDP port.

It is **read-only**: it searches and reads listings, it cannot buy.

## Tools

| Tool | Purpose |
|---|---|
| `search_aliexpress` | Keyword search with optional `sort` (`orders`, `price_asc`, `price_desc`, `newest`), `min_price` / `max_price`, and `page`. Returns products with `id`, title, price, currency, rating, orders-sold, image and URL. |
| `get_aliexpress_product` | Full record for a numeric product id **or** a full product URL: title, selected + per-variant prices, rating, review count, orders, stock, store, shipping, SKU option axes, specs and all images. Served via MTop or the browser transport (`source: "mtop"` / `"browser"`, `partial: false`). If both are unavailable it degrades to `source: "ssr+search"`, `partial: true` — title, images, url, price, rating and orders, with everything else named in `unavailable`. |

Responses are shaped as clean dicts (`{query, returned, total, items: [...]}` for
search). On an anti-bot block or a transport error the tool returns
`{"error": "..."}` rather than raising.

### How product detail is fetched (and why a browser)

AliExpress can gate direct MTop calls with `FAIL_SYS_USER_VALIDATE` /
`RGV587_ERROR`. Detail retains three transports, cheapest first:

| # | Transport | Result |
|---|---|---|
| 1 | Direct MTop over HTTP | Tried outside the existing anti-bot cooldown |
| 2 | **Obscura** — load the page and request JSON through its native MTop SDK | Full record, `source: "browser"`, `partial: false` |
| 3 | SSR composite — product page + search results | Explicitly partial, `source: "ssr+search"`, `partial: true` |

The browser uses only parameters from the real first-party PDP request, not a
custom signer, copied token or fabricated API request. JSON avoids Obscura's
cross-origin dynamic-script credential limitation while preserving the site's
SDK bootstrap and refresh behavior. Only an explicit MTop `SUCCESS` reaches
the unchanged rich-product parser; SDK errors and timeouts retain the existing
fallback semantics.

Each attempt uses a fresh context with the configured locale, timezone,
viewport and shared `aep_usuc_f` market cookie. Routed `Accept-Language` headers
also reach Obscura's stealth HTTP client. Navigation, request observation and
SDK completion share one per-attempt deadline. Existing bounded attempts and
backoff remain; clear punish pages and HTTP 429 stop immediately.

The Docker image verifies the official render-enabled stealth archive's SHA-256
before installing both CLI and worker binaries (Linux amd64/arm64). The worker
starts CDP on an ephemeral loopback port with a 30-second startup limit, reaps
its entire process group on restart/shutdown and suppresses provider console
output, which can contain credentials. Native runtime requirements are CA
certificates, libstdc++ and glibc 2.34 (amd64) / 2.35 (arm64); Debian bookworm supplies these.

The CLI entrypoint handles SIGTERM for both HTTP and stdio, including Uvicorn's
captured-signal re-raise. Shutdown first disables restarts and stops the owned
process group (TERM, then KILL within three seconds), before draining the
browser worker. Signal handlers are installed only while `server.main()` runs
and restored afterward; embedding callers must explicitly call
`browser.shutdown()`. An uncatchable SIGKILL still requires supervisor/cgroup
cleanup.

For a local install, provision the same official binary pair together and set
`AE_OBSCURA_BINARY` to the CLI path. The default is `/opt/obscura/obscura`.

## Market (Germany / EUR by default)

Prices, currency and localisation follow the target market, set by three env
vars (defaults in **bold**):

| Env var | Default | Meaning |
|---|---|---|
| `AE_REGION` | **`DE`** | Ship-to region |
| `AE_CURRENCY` | **`EUR`** | Display currency |
| `AE_LOCALE` | **`de_DE`** | Language / localisation |
| `AE_MAX_CONCURRENT` | **`2`** | Process-wide cap on concurrent requests to AliExpress (0.1.1+). Chat agents fire several tool calls in parallel; the excess queue instead of hitting AliExpress at once, which is what trips its anti-bot (x5sec / TMD). |
| `AE_MTOP_COOLDOWN` | **`900`** | Seconds to stop attempting MTop product detail after it answers with an anti-bot challenge (0.2.0+). Without it every detail call burns four blocked round-trips before falling back. Set `0` to retry MTop on every call. |
| `AE_BROWSER_ENABLED` | **`true`** | Browser transport for product detail (0.3.0+). Set `false` to run browser-less; detail then degrades to the partial `ssr+search` record. |
| `AE_BROWSER_ATTEMPTS` | **`3`** | Attempts per lookup, each in a fresh context. AliExpress challenges a proportion of loads; a retry usually clears it. |
| `AE_BROWSER_RETRY_DELAY_S` | **`1.5`** | Pause between attempts. Retrying instantly is what the anti-bot watches for. |
| `AE_BROWSER_TIMEOUT_MS` | **`45000`** | Per-attempt navigation + SDK wall-clock budget; also sets Obscura's navigation deadline. |
| `AE_OBSCURA_BINARY` | **`/opt/obscura/obscura`** | Official render-enabled stealth CLI, alongside its `obscura-worker` binary. |
| `AE_TIMEZONE` | **`Europe/Berlin`** | Timezone the browser context impersonates; tune it when targeting a non-German market. |
| `AE_VIEWPORT_WIDTH` | **`1440`** | Browser viewport width. |
| `AE_VIEWPORT_HEIGHT` | **`900`** | Browser viewport height. |

These are pushed to AliExpress via the `aep_usuc_f` cookie (search) and the
`_lang` / `_currency` / `country` MTop params (product detail). Any market the
site supports works; it falls back to site defaults for anything it doesn't.

## Run

```bash
docker run --rm -p 8000:8000 ghcr.io/jnslmk/aliexpress-mcp:latest
# streamable-HTTP MCP endpoint: http://127.0.0.1:8000/mcp
# health:                       http://127.0.0.1:8000/healthz
```

Transport is streamable-HTTP by default (`MCP_TRANSPORT=http`, `:8000/mcp`); set
`MCP_TRANSPORT=stdio` for a classic stdio MCP server. See `.env.example`.

`/healthz` always returns `200 {"status":"ok", ...}` while the process is up —
there is no credential or hard dependency to gate on. A blocked AliExpress
upstream is a per-request condition surfaced in the tool response, not container
ill-health.

## Caveats

This talks to **undocumented** AliExpress endpoints. That is inherent to the
problem (there is no official API), but it means:

- AliExpress can change the embedded-JSON shape or the MTop signing scheme at any
  time and break extraction. Every extractor is written defensively and failures
  degrade to a clear error.
- **Datacenter IPs are challenged more aggressively than residential ones.** From
  some hosts AliExpress returns an anti-bot (`x5sec` / `RGV587` / TMD punish)
  response to *search*. Search tries the same cheap plain-HTTP call first and,
  on a TMD punish page, falls back to the browser transport loading the
  identical search URL — the same trade AliExpress applies to product detail.
  If the browser gets challenged too, or `AE_BROWSER_ENABLED=false`, the tool
  reports a block.
- **A residential IP is not immunity — volume still trips the block.** While
  developing 0.2.0 a burst of exploratory requests earned a TMD punish page on a
  residential connection that lasted well over an hour, taking `search` down with
  it. `AE_MAX_CONCURRENT` (default `2`) caps concurrency, but nothing caps your
  total rate; if you are probing, go slowly and expect a long cooldown when you
  get it wrong.
- Be a good citizen: low request volume only.

## Credits

The AliExpress access approach (MTop token bootstrap + MD5 signing, and
`_init_data_` search parsing) is ported from
[Averyy/fetchaller-mcp](https://github.com/Averyy/fetchaller-mcp) (MIT).
Transport/packaging mirror the sibling [jnslmk/ebay-mcp](https://github.com/jnslmk/ebay-mcp).

## License

MIT — see [LICENSE](LICENSE).
