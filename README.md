# AliExpress MCP

A self-hosted [MCP](https://modelcontextprotocol.io) server that lets an LLM
**search AliExpress and inspect product listings** — with **no API key**.

AliExpress has no official public product-search API, so this server mirrors what
the aliexpress.com web frontend does:

- **Search** — fetches the server-rendered search page
  (`/w/wholesale-<query>.html`) and pulls the product list out of the
  `_init_data_` JSON the page embeds for its own hydration. On an anti-bot
  (TMD) punish page it falls back to loading the same URL in a headless
  browser.
- **Product detail** — tries AliExpress's internal **MTop** API
  (`acs.aliexpress.com`) first, which needs an `_m_h5_tk` token (bootstrapped on
  the first request) and an MD5 request signature `MD5(token & timestamp &
  appKey & data)`. MTop is anti-bot gated for plain HTTP clients today, so a
  headless Chromium loads the product page and the server intercepts the very
  same MTop response the page fetches for itself. See
  [how product detail is fetched](#how-product-detail-is-fetched-and-why-a-browser).

TLS fingerprinting via [`curl_cffi`](https://github.com/lexiforest/curl_cffi)
(Chrome impersonation) is usually enough for search, which tries a plain HTTP
call first. Product detail is gated on executed JavaScript rather than TLS
fingerprint, so it needs a real browser (headless Chromium, via
[`patchright`](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)); search
falls back to the same browser transport when its plain HTTP call is
anti-bot challenged.

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

As of **2026-08-08** AliExpress answers the MTop product-detail endpoints
(`mtop.aliexpress.pdp.pc.query`, `…itemdetail.pc.asyncPCDetail`) with
`FAIL_SYS_USER_VALIDATE` / `RGV587_ERROR` and a captcha url instead of data,
when called over plain HTTP. Four things were checked rather than assumed:

- **Not an IP-reputation problem.** A residential IP is refused exactly like a
  datacenter one, in the same minute.
- **Not a token or signing regression.**
  `mtop.relationrecommend.aliexpressrecommend.recommend` still mints an
  `_m_h5_tk` normally, and signing the pdp call with that fresh token is
  refused just the same.
- **Not login-gated, and the endpoint is not dead.** A real Chromium — *not*
  logged in, on the *same IP*, and headless at that — gets `SUCCESS` and a
  ~95 KB payload from that exact endpoint. What the anti-bot wants is the
  JavaScript executed; `curl_cffi`'s Chrome TLS impersonation is not enough on
  its own.
- **The product page cannot stand in for it.** `/item/<id>.html` is now
  client-side rendered, ships an empty `window.runParams`, and fetches its own
  data from that same endpoint.

So detail uses three transports, cheapest first:

| # | Transport | Result |
|---|---|---|
| 1 | Direct MTop over HTTP | Currently gated. Still tried first, so the cheap path resumes automatically if AliExpress ungates it |
| 2 | **Browser** — load the page, intercept its own `pdp.pc.query` response | **Full record.** ~2 s warm. `source: "browser"` |
| 3 | SSR composite — product page + search results | Partial. Only if the browser is disabled or fails. `source: "ssr+search"`, `partial: true` |

Transport 2 does not reimplement the anti-bot JavaScript; it just reads the
answer the page already obtains for itself. The payload is byte-identical to
what direct MTop used to return, so the same parser handles it unchanged.

Two behaviours here were measured, not guessed, and both are counter-intuitive:

- **A fresh browser context per lookup, not a warm one.** Reusing a context got
  the *second* back-to-back lookup answered with RGV587, while the first request
  of a fresh context succeeds. Carried-over state is what marks you.
- **Bail out the instant RGV587 arrives.** The page will not retry itself, so
  waiting out the timeout buys nothing — detecting it and retrying in a fresh
  context turns a 45 s dead wait into a ~2 s retry. AliExpress challenges a
  *proportion* of loads rather than locking on, so a capped retry
  (`AE_BROWSER_ATTEMPTS`, default 3) recovers almost all of them.

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
| `AE_BROWSER_TIMEOUT_MS` | **`45000`** | Per-attempt budget. Rarely reached: a challenged attempt aborts as soon as RGV587 arrives. |
| `AE_BROWSER_HEADLESS` | **`true`** | Headless suffices for AliExpress (verified). Unlike the sibling baumarkt-mcp, no Xvfb/headed display is needed. |

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
