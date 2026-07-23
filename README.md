# AliExpress MCP

A self-hosted [MCP](https://modelcontextprotocol.io) server that lets an LLM
**search AliExpress and inspect product listings** — with **no API key**.

AliExpress has no official public product-search API, so this server mirrors what
the aliexpress.com web frontend does:

- **Search** — fetches the server-rendered search page
  (`/w/wholesale-<query>.html`) and pulls the product list out of the
  `_init_data_` JSON the page embeds for its own hydration.
- **Product detail** — calls AliExpress's internal **MTop** API
  (`acs.aliexpress.com`), which needs an `_m_h5_tk` token (bootstrapped on the
  first request) and an MD5 request signature `MD5(token & timestamp & appKey &
  data)`. The token is auto-refreshed on expiry.

TLS fingerprinting via [`curl_cffi`](https://github.com/lexiforest/curl_cffi)
(Chrome impersonation) lets a head-less, browser-less container talk to
AliExpress without immediately tripping bot detection — so the image stays tiny
and runs read-only.

It is **read-only**: it searches and reads listings, it cannot buy.

## Tools

| Tool | Purpose |
|---|---|
| `search_aliexpress` | Keyword search with optional `sort` (`orders`, `price_asc`, `price_desc`, `newest`), `min_price` / `max_price`, and `page`. Returns products with `id`, title, price, currency, rating, orders-sold, image and URL. |
| `get_aliexpress_product` | Full record for a numeric product id **or** a full product URL: title, selected + per-variant prices, rating, review count, orders, stock, store, shipping, SKU option axes, specs and all images. |

Responses are shaped as clean dicts (`{query, returned, total, items: [...]}` for
search). On an anti-bot block or a transport error the tool returns
`{"error": "..."}` rather than raising.

## Market (Germany / EUR by default)

Prices, currency and localisation follow the target market, set by three env
vars (defaults in **bold**):

| Env var | Default | Meaning |
|---|---|---|
| `AE_REGION` | **`DE`** | Ship-to region |
| `AE_CURRENCY` | **`EUR`** | Display currency |
| `AE_LOCALE` | **`de_DE`** | Language / localisation |

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
  some hosts AliExpress may return an anti-bot (`x5sec` / `RGV587` / TMD punish)
  response and the tool will report a block. There is no browser fallback in this
  build (kept deliberately browser-less); if your host is hard-blocked, run it
  from a residential connection.
- Be a good citizen: low request volume only.

## Credits

The AliExpress access approach (MTop token bootstrap + MD5 signing, and
`_init_data_` search parsing) is ported from
[Averyy/fetchaller-mcp](https://github.com/Averyy/fetchaller-mcp) (MIT).
Transport/packaging mirror the sibling [jnslmk/ebay-mcp](https://github.com/jnslmk/ebay-mcp).

## License

MIT — see [LICENSE](LICENSE).
