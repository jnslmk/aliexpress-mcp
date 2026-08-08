"""AliExpress client — unofficial, key-less.

There is no official public AliExpress product-search API, so this client mirrors
what the aliexpress.com web frontend does, using two undocumented-but-stable
mechanisms (ported from Averyy/fetchaller-mcp, MIT):

* **Search** — fetch the server-rendered search page
  (``/w/wholesale-<slug>.html``) and pull the product list out of the
  ``_init_data_`` JSON blob the page embeds for its own hydration.

* **Product detail** — three transports, cheapest first:

  1. AliExpress's internal **MTop** API (``acs.aliexpress.com``), which returns
     the rich record (per-variant pricing, store, shipping, specs). MTop needs a
     ``_m_h5_tk`` token the site hands out on a deliberately unsigned request,
     plus an MD5 signature ``MD5(token & timestamp & appKey & data)``.

  2. A **browser** that loads the product page and hands back the very same
     MTop response the page fetches for itself. See ``browser.py``.

  3. An **SSR composite fallback** for when the browser is unavailable. See
     ``_get_product_via_ssr``.

  As of 2026-08-08 path 1 answers ``FAIL_SYS_USER_VALIDATE / RGV587_ERROR``
  with a captcha url instead of data. Four things were verified rather than
  assumed while pinning that down:

  * **Not IP reputation.** A residential IP is refused in the same minute as a
    datacenter one.
  * **Not a token/signing regression.** ``mtop.relationrecommend...`` still
    mints a ``_m_h5_tk`` normally, and signing the pdp call with that fresh
    token is refused just the same.
  * **Not login-gated, and not the endpoint being dead.** A real Chromium, not
    logged in, on the *same IP*, gets ``SUCCESS`` and a ~95 KB payload from that
    exact endpoint. What the anti-bot wants is the JavaScript executed —
    ``curl_cffi``'s Chrome TLS impersonation is not enough on its own.
  * **The product page cannot stand in for it.** ``/item/<id>.html`` is now
    client-side rendered: ``window.runParams`` ships **empty** and the page
    fetches its own data from that same MTop endpoint.

  Hence path 2, which is not a reimplementation of the anti-bot JS but simply
  a reader of the answer the page already obtains. The payload is identical, so
  ``_extract_product`` parses it unchanged.

All of this is fragile by nature — AliExpress can change the embedded-JSON
shape, the MTop signing scheme, or start returning anti-bot (``x5sec`` /
``RGV587``) challenges at any time. Every extractor is written defensively and
failures degrade to a clear, partial answer rather than a crash.

TLS fingerprinting via ``curl_cffi`` (Chrome impersonation) is what lets a
head-less, browser-less container talk to AliExpress without immediately
tripping bot detection — no Selenium/Chromium needed, so the container stays
tiny and its root filesystem read-only.

Market defaults to Germany / EUR / de_DE; override via ``AE_REGION`` /
``AE_CURRENCY`` / ``AE_LOCALE``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from html import unescape
from typing import Any, Optional
from urllib.parse import quote

from curl_cffi import requests

from aliexpress_mcp import browser

log = logging.getLogger("aliexpress-mcp")

# --------------------------------------------------------------------------- #
# market configuration
# --------------------------------------------------------------------------- #

REGION = os.getenv("AE_REGION", "DE")
CURRENCY = os.getenv("AE_CURRENCY", "EUR")
LOCALE = os.getenv("AE_LOCALE", "de_DE")
IMPERSONATE = os.getenv("AE_IMPERSONATE", "chrome")

# The cookie AliExpress reads for ship-to region, display currency and locale.
# Setting it makes both the SSR search page and the MTop API answer in EUR / de_DE.
_USUC_COOKIE = f"site=glo&c_tp={CURRENCY}&region={REGION}&b_locale={LOCALE}"

_BASE_HEADERS = {
    "Accept-Language": f"{LOCALE.replace('_', '-')},{LOCALE.split('_')[0]};q=0.9,en;q=0.8",
    "Referer": "https://www.aliexpress.com/",
}

# --------------------------------------------------------------------------- #
# MTop constants
# --------------------------------------------------------------------------- #

_MTOP_BASE = "https://acs.aliexpress.com"
_APP_KEY = "12574478"
_TOKEN_TTL = 3000  # seconds (~50 min); AliExpress issues ~60 min tokens
# Product-detail APIs, tried in order. pdp.pc.query is the modern PC endpoint.
_MTOP_APIS = [
    ("mtop.aliexpress.pdp.pc.query", "1.0"),
    ("mtop.aliexpress.itemdetail.pc.asyncPCDetail", "1.0"),
]

# Token minter. The product-detail endpoints above refuse to mint a token while
# they are gated (they answer RGV587 instead of the usual FAIL_SYS_TOKEN_EMPTY),
# which used to leave the client with no token at all and a misleading
# "bootstrap did not yield a cookie" warning. This endpoint is not gated and
# still issues one, so token acquisition is decoupled from the API being called.
_MTOP_TOKEN_MINTER = ("mtop.relationrecommend.aliexpressrecommend.recommend", "1.0")

# How long to stop trying MTop after it answers with an anti-bot challenge.
# Without this every detail call burns four blocked round-trips (two APIs, each
# preceded by a token bootstrap) before falling back — slow, and pointless
# extra traffic to an endpoint that has already said no.
_MTOP_COOLDOWN = int(os.getenv("AE_MTOP_COOLDOWN", "900"))
_mtop_blocked_until: float = 0.0
_mtop_block_lock = threading.Lock()

_OG_SUFFIX_RE = re.compile(r"\s*-\s*AliExpress(?:\s+\d+)?\s*$", re.I)

_PRODUCT_ID_RE = re.compile(r"(?:aliexpress\.com/item/|(?<!\d))(\d{8,20})(?!\d)")


def extract_product_id(value: str) -> Optional[str]:
    """Pull the numeric product id out of a bare id or any AliExpress URL."""
    m = _PRODUCT_ID_RE.search(value or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# shared curl_cffi session (thread-safe)
# --------------------------------------------------------------------------- #

_session: Optional[requests.Session] = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session(impersonate=IMPERSONATE)
                s.headers.update(_BASE_HEADERS)
                s.cookies.set("aep_usuc_f", _USUC_COOKIE, domain=".aliexpress.com")
                _session = s
    return _session


# Process-wide cap on concurrent AliExpress interactions. Chat agents fire
# several tool calls in parallel, and a burst of simultaneous requests from a
# datacenter IP is the fastest way to trip AliExpress's anti-bot (x5sec / TMD
# punish pages — observed from this very host). FastMCP runs the sync tools in
# a thread pool, so this is a threading gate, not an asyncio one: excess calls
# queue here instead of hitting AliExpress at once.
MAX_CONCURRENT = int(os.getenv("AE_MAX_CONCURRENT", "2"))
_gate = threading.BoundedSemaphore(MAX_CONCURRENT)


# --------------------------------------------------------------------------- #
# embedded-JSON extraction (SSR search page)
# --------------------------------------------------------------------------- #


def _extract_json_object(html: str, start: int, max_scan: int = 3_000_000) -> Optional[dict]:
    """String-aware brace counting to slice one JSON object out of HTML.

    Regex is unreliable on the 400 KB+ ``_init_data_`` payload, so we walk from
    the opening brace tracking string state until the matching close brace.
    """
    depth = 0
    in_string = False
    escape = False
    end = min(start + max_scan, len(html))
    for i in range(start, end):
        ch = html[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_init_data(html: str) -> Optional[dict]:
    """Locate and parse the ``_init_data_`` object embedded in a search page."""
    # Strategy 1: HTML comment markers around the hydration script (most reliable).
    start_idx = html.find("init-data-start")
    end_idx = html.find("init-data-end")
    if start_idx != -1 and end_idx != -1:
        data_offset = html.find("data:", start_idx)
        if data_offset != -1 and data_offset < end_idx:
            json_start = html.find("{", data_offset + 5)
            if json_start != -1 and json_start < end_idx:
                result = _extract_json_object(html, json_start, end_idx - json_start + 100)
                if result is not None:
                    return result
    # Strategy 2: direct ``_dida_config_._init_data_=`` assignment.
    assign_idx = html.find("_dida_config_._init_data_=")
    if assign_idx == -1:
        return None
    data_idx = html.find("data:", assign_idx + 26)
    if data_idx == -1 or data_idx - assign_idx > 50:
        return None
    json_start = html.find("{", data_idx + 5)
    if json_start == -1:
        return None
    return _extract_json_object(html, json_start, 2_000_000)


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

_SORT_MAP = {
    None: None,
    "default": None,
    "relevance": None,
    "orders": "total_tranpro_desc",
    "sold": "total_tranpro_desc",
    "price_asc": "price_asc",
    "price_desc": "price_desc",
    "newest": "create_desc",
}


def _img_url(raw: Optional[str]) -> Optional[str]:
    """Normalise a protocol-relative AliExpress CDN url to https."""
    if not raw:
        return None
    if raw.startswith("//"):
        return "https:" + raw
    return raw


def _parse_search_item(product: dict) -> dict:
    """Flatten one ``_init_data_`` product entry into a clean dict."""
    pid = str(product.get("productId") or product.get("redirectedId") or "")

    title_mod = product.get("title") or {}
    if isinstance(title_mod, dict):
        title = title_mod.get("displayTitle") or title_mod.get("seoTitle") or ""
    else:
        title = str(title_mod)

    prices = product.get("prices") or {}
    sale = prices.get("salePrice") or {}
    original = prices.get("originalPrice") or {}

    evaluation = product.get("evaluation") or {}
    trade = product.get("trade") or {}
    image = product.get("image") or {}

    return {
        "id": pid,
        "title": title,
        "price": sale.get("minPrice"),
        "price_formatted": sale.get("formattedPrice"),
        "currency": sale.get("currencyCode") or CURRENCY,
        "original_price": original.get("minPrice"),
        "discount_pct": sale.get("discount"),
        "rating": evaluation.get("starRating"),
        "orders": trade.get("tradeDesc"),
        "image": _img_url(image.get("imgUrl")),
        "url": f"https://www.aliexpress.com/item/{pid}.html" if pid else None,
    }


def search(
    query: str,
    limit: int = 10,
    sort: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    page: int = 1,
) -> dict:
    """Search AliExpress via the SSR search page. Raises AliExpressError on block."""
    with _gate:
        return _search(
            query=query,
            limit=limit,
            sort=sort,
            min_price=min_price,
            max_price=max_price,
            page=page,
        )


def _search(
    query: str,
    limit: int = 10,
    sort: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    page: int = 1,
) -> dict:
    """Search AliExpress via the SSR search page. Raises AliExpressError on block."""
    query_slug = quote((query or "").strip().replace(" ", "-"), safe="-")
    url = f"https://www.aliexpress.com/w/wholesale-{query_slug}.html"
    params = {"page": str(max(1, page))}
    sort_type = _SORT_MAP.get(sort, None)
    if sort_type:
        params["sortType"] = sort_type
    if min_price is not None:
        params["minPrice"] = str(min_price)
    if max_price is not None:
        params["maxPrice"] = str(max_price)

    session = _get_session()
    try:
        resp = session.get(url, params=params, timeout=30)
    except Exception as exc:  # noqa: BLE001 - surface any transport error cleanly
        raise AliExpressError(f"search request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise AliExpressError(f"search returned HTTP {resp.status_code}")

    html = resp.text
    init_data = _extract_init_data(html)
    if not init_data:
        if "_____tmd_____" in html or "punish" in html.lower():
            raise AliExpressError(
                "blocked by AliExpress anti-bot (TMD challenge) — the search page "
                "returned a punish/verification page instead of results."
            )
        raise AliExpressError(
            "could not locate product data in the search page "
            f"(received {len(html)} chars of HTML)."
        )

    try:
        root_fields = init_data["data"]["root"]["fields"]
        mods = root_fields.get("mods", {})
        raw_items = mods.get("itemList", {}).get("content", []) or []
        page_info = root_fields.get("pageInfo", {})
        total = page_info.get("totalResults", len(raw_items))
        cur_page = page_info.get("page", page)
    except (KeyError, TypeError) as exc:
        raise AliExpressError(f"unexpected search data structure: {exc}") from exc

    items = [_parse_search_item(p) for p in raw_items[: max(1, limit)]]
    return {
        "query": query,
        "region": REGION,
        "currency": CURRENCY,
        "page": cur_page,
        "total": total,
        "returned": len(items),
        "items": items,
    }


# --------------------------------------------------------------------------- #
# MTop client (product detail)
# --------------------------------------------------------------------------- #

_token: str = ""
_token_time: float = 0.0
_token_lock = threading.Lock()


class AliExpressError(RuntimeError):
    """Raised when AliExpress blocks the request or returns no usable data."""


def _sign(token: str, timestamp: str, data_str: str) -> str:
    return hashlib.md5(f"{token}&{timestamp}&{_APP_KEY}&{data_str}".encode()).hexdigest()


def _token_expired() -> bool:
    return not _token or (time.time() - _token_time) > _TOKEN_TTL


def _mtop_get(api: str, version: str, data_dict: dict) -> dict:
    """One signed MTop GET. Updates the shared token from the response cookie."""
    global _token, _token_time
    session = _get_session()
    timestamp = str(int(time.time() * 1000))
    data_str = json.dumps(data_dict, separators=(",", ":"))
    sign = _sign(_token, timestamp, data_str)
    url = f"{_MTOP_BASE}/h5/{api}/{version}/"
    params = {
        "jsv": "2.5.1",
        "appKey": _APP_KEY,
        "t": timestamp,
        "sign": sign,
        "api": api,
        "v": version,
        "timeout": "5000",
        "type": "originaljson",
        "dataType": "json",
        "data": data_str,
    }
    resp = session.get(url, params=params, timeout=15)

    tk = resp.cookies.get("_m_h5_tk")
    if tk:
        new_token = tk.split("_")[0]
        if new_token != _token:
            _token = new_token
            _token_time = time.time()

    body = resp.text
    jsonp = re.match(r"^\s*\w+\(([\s\S]+)\)\s*;?\s*$", body)
    if jsonp:
        body = jsonp.group(1)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ret": ["PARSE_ERROR"], "data": {}}


def _bootstrap_token() -> None:
    """Get an initial ``_m_h5_tk`` by making one deliberately unsigned request.

    The server answers FAIL_SYS_TOKEN_EMPTY and sets the token cookie, which the
    signed request then uses. Only real API endpoints set the cookie.

    This deliberately asks ``_MTOP_TOKEN_MINTER`` rather than the product-detail
    API we are about to call: a gated endpoint answers the unsigned bootstrap
    with RGV587 and sets no cookie at all, so bootstrapping against it fails
    even when nothing is wrong with tokens.
    """
    global _token, _token_time
    with _token_lock:
        if not _token_expired():
            return
        _token = ""
        _mtop_get(*_MTOP_TOKEN_MINTER, {})
        if not _token:
            log.warning("MTop token bootstrap did not yield an _m_h5_tk cookie")


def _ret_str(result: dict) -> str:
    ret = result.get("ret", [])
    return " ".join(ret) if isinstance(ret, list) else str(ret)


def _mtop_request(api: str, version: str, data_dict: dict) -> dict:
    """Signed MTop request with token bootstrap + one refresh on token expiry."""
    if _token_expired():
        _bootstrap_token()

    result = _mtop_get(api, version, data_dict)
    ret = _ret_str(result)

    if "FAIL_SYS_TOKEN_EXPIRED" in ret or "FAIL_SYS_TOKEN_EXOIRED" in ret or "FAIL_SYS_TOKEN_EMPTY" in ret:
        with _token_lock:
            global _token
            _token = ""
        _bootstrap_token()
        result = _mtop_get(api, version, data_dict)

    return result


# --------------------------------------------------------------------------- #
# product-detail extraction
# --------------------------------------------------------------------------- #


def _extract_product(result: dict) -> Optional[dict]:
    """Turn a pdp.pc.query MTop response into a clean product dict."""
    data = result.get("data", {})
    r = data.get("result", data)
    if not isinstance(r, dict):
        return None

    global_data = (r.get("GLOBAL_DATA") or {}).get("globalData", {})

    title_mod = r.get("PRODUCT_TITLE") or {}
    title = title_mod.get("text") or global_data.get("subject") or ""
    if not title:
        return None

    # --- pricing (selected sku + all variants) ---
    price_mod = r.get("PRICE") or {}
    sku_price_map = price_mod.get("skuIdStrPriceInfoMap") or {}
    selected_sku = str(price_mod.get("selectedSkuId") or "")

    def _price_entry(entry: dict) -> dict:
        orig = entry.get("originalPrice") or {}
        return {
            "sale_price": entry.get("salePriceString"),
            "original_price": orig.get("formatedAmount"),
            "original_value": orig.get("value"),
            "currency": orig.get("currency") or CURRENCY,
        }

    price: dict = {}
    variants_pricing = []
    for sku_id, entry in list(sku_price_map.items())[:25]:
        pe = _price_entry(entry)
        pe["sku_id"] = sku_id
        variants_pricing.append(pe)
        if sku_id == selected_sku or not price:
            price = {k: v for k, v in pe.items() if k != "sku_id"}

    # --- rating / orders ---
    rating_mod = r.get("PC_RATING") or {}
    rating = rating_mod.get("rating")
    review_count = rating_mod.get("totalValidNum")
    orders = rating_mod.get("otherText")

    # --- store ---
    shop = r.get("SHOP_CARD_PC") or {}
    seller_info = shop.get("sellerInfo") or {}
    store = {
        "name": shop.get("storeName"),
        "positive_rate": shop.get("sellerPositiveRate"),
        "score": shop.get("sellerScore"),
        "country": seller_info.get("countryCompleteName"),
        "opened_year": seller_info.get("openedYear"),
        "url": _img_url(seller_info.get("storeURL")),
    }

    # --- images ---
    img_mod = r.get("HEADER_IMAGE_PC") or {}
    images = [i for i in (img_mod.get("imagePathList") or []) if i][:12]

    # --- shipping (best effort) ---
    shipping: dict = {}
    ship_mod = r.get("SHIPPING") or {}
    oll = ship_mod.get("originalLayoutResultList") or []
    if oll and isinstance(oll, list):
        biz = (oll[0] or {}).get("bizData") or {}
        shipping = {
            "ship_from": biz.get("shipFrom"),
            "ship_to": biz.get("shipToCode"),
            "delivery_days_max": biz.get("deliveryDayMax"),
            "eta": biz.get("displayEtaMinDate"),
            "amount": biz.get("displayAmount") or biz.get("shippingFee"),
        }

    # --- variants (sku options) ---
    variants = []
    sku_mod = r.get("SKU") or {}
    for prop in sku_mod.get("skuProperties") or []:
        name = prop.get("skuPropertyName")
        values = [
            v.get("propertyValueDisplayName") or v.get("propertyValueName")
            for v in prop.get("skuPropertyValues") or []
        ]
        values = [v for v in values if v]
        if name and values:
            variants.append({"name": name, "values": values[:30]})

    # --- specifications ---
    specs = {}
    prop_mod = r.get("PRODUCT_PROP_PC") or {}
    for s in (prop_mod.get("showedProps") or prop_mod.get("outerProps") or [])[:30]:
        name = s.get("attrName") or s.get("name")
        value = s.get("attrValue") or s.get("value")
        if name and value:
            specs[name] = value

    # --- stock ---
    qty_mod = r.get("QUANTITY_PC") or {}
    stock = qty_mod.get("totalAvailableInventory")

    pid = str(global_data.get("productId") or "")
    return {
        "id": pid,
        "title": title,
        "url": f"https://www.aliexpress.com/item/{pid}.html" if pid else None,
        "price": price,
        "rating": rating,
        "review_count": review_count,
        "orders": orders,
        "stock": stock,
        "store": store,
        "shipping": shipping,
        "variants": variants,
        "variants_pricing": variants_pricing,
        "specs": specs,
        "images": images,
        "category_path": global_data.get("categoryPath"),
    }


def get_product(product: str) -> dict:
    """Fetch full product detail for a numeric id or AliExpress URL."""
    with _gate:
        return _get_product(product)


def _mtop_in_cooldown() -> bool:
    return time.time() < _mtop_blocked_until


def _mark_mtop_blocked() -> None:
    global _mtop_blocked_until
    with _mtop_block_lock:
        _mtop_blocked_until = time.time() + _MTOP_COOLDOWN


def _get_product_via_mtop(pid: str) -> Optional[dict]:
    """Rich product record via MTop, or ``None`` if MTop cannot serve it.

    Raises only for a definitive *product-level* answer (not found). An
    anti-bot refusal returns ``None`` after arming the cooldown, so the caller
    can fall back instead of failing the whole request.
    """
    data = {
        "productId": pid,
        "_lang": LOCALE,
        "_currency": CURRENCY,
        "country": REGION,
        "clientType": "pc",
    }

    for api, version in _MTOP_APIS:
        try:
            result = _mtop_request(api, version, data)
        except Exception as exc:  # noqa: BLE001
            log.debug("MTop %s transport error: %s", api, exc)
            continue
        ret = _ret_str(result)
        if "SUCCESS" in ret:
            inner = (result.get("data", {}).get("result", {}) or {})
            gd = (inner.get("GLOBAL_DATA") or {}).get("globalData", {})
            if gd.get("errorCode") == "SITEM_NOT_EXIST":
                raise AliExpressError(f"product {pid} not found (delisted or unavailable)")
            extracted = _extract_product(result)
            if extracted and extracted.get("title"):
                return extracted
            continue
        if "FAIL_SYS_USER_VALIDATE" in ret or "RGV587_ERROR" in ret:
            _mark_mtop_blocked()
            log.info(
                "MTop product detail is anti-bot gated (%s); using the SSR "
                "fallback and skipping MTop for %ss",
                ret.split("::")[0],
                _MTOP_COOLDOWN,
            )
            return None
    return None


def _item_page_identity(pid: str) -> dict:
    """Title, images and canonical url straight off the product page.

    The page is client-side rendered, so this reads the ``og:`` meta tags and
    the ``_d_c_.DCData`` image list the server still emits — the only product
    facts left in the HTML. Deliberately not parsed for price: it is not there.
    """
    session = _get_session()
    url = f"https://www.aliexpress.com/item/{pid}.html"
    try:
        resp = session.get(url, timeout=30)
    except Exception as exc:  # noqa: BLE001
        raise AliExpressError(f"product page request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise AliExpressError(f"product page returned HTTP {resp.status_code}")

    html_text = resp.text
    if "_____tmd_____" in html_text or "x5secdata" in html_text:
        raise AliExpressError(
            "blocked by AliExpress anti-bot — the product page returned a "
            "verification page instead of the listing."
        )

    def _meta(prop: str) -> Optional[str]:
        m = re.search(
            r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % re.escape(prop),
            html_text,
        )
        return unescape(m.group(1)) if m else None

    title = _meta("og:title") or ""
    # og:title carries a " - AliExpress <sellerId>" suffix that is not part of
    # the listing name and would poison the search query built from it below.
    title = _OG_SUFFIX_RE.sub("", title).strip()
    if not title:
        raise AliExpressError(
            f"product {pid} not found, or its page no longer exposes a title"
        )

    images: list[str] = []
    m = re.search(r"window\._d_c_\.DCData\s*=\s*\{", html_text)
    if m:
        blob = _extract_json_object(html_text, m.end() - 1, 200_000) or {}
        images = [i for i in (blob.get("imagePathList") or []) if i][:12]
    if not images:
        main = _meta("og:image")
        images = [main] if main else []

    return {"title": title, "images": images, "url": url}


def _find_in_search(pid: str, title: str) -> tuple[Optional[dict], str]:
    """Locate this product among search results to recover its commercial data.

    Returns ``(hit, status)`` where status is ``matched``, ``not_found`` or
    ``blocked``. The distinction is not cosmetic: "we searched and this listing
    was not in the results" and "we never got to search" need different fixes,
    and collapsing them into one empty answer is how a transient block gets
    misread as a parser regression.

    The full listing title is a poor query — AliExpress slugifies it into a very
    long URL and recall drops — so this tries a trimmed prefix first and only
    then a shorter one. Two searches maximum: this runs inside the concurrency
    gate, and the point of the fallback is to be cheap.
    """
    words = title.split()
    queries = [" ".join(words[:12])]
    if len(words) > 6:
        queries.append(" ".join(words[:6]))

    for query in queries:
        try:
            # _search, never search: we already hold _gate and it is not
            # reentrant, so calling the public wrapper would deadlock.
            result = _search(query, limit=40)
        except AliExpressError as exc:
            # Logged at INFO, not debug: without it an operator sees a record
            # missing its price and no reason anywhere for why.
            log.info("product-detail fallback search was blocked (%r): %s", query, exc)
            return None, "blocked"
        for item in result.get("items") or []:
            if item.get("id") == pid:
                return item, "matched"
    log.info("product-detail fallback searched but did not find %s in results", pid)
    return None, "not_found"


def _get_product_via_ssr(pid: str, mtop_note: str) -> dict:
    """Compose a product record from the sources that still work.

    Identity (title, images, url) comes from the product page; the commercial
    fields (price, rating, orders) from finding the same id in search results.
    Anything MTop alone could supply is reported as unavailable rather than
    silently omitted, so a caller cannot mistake a partial record for a full one.
    """
    identity = _item_page_identity(pid)
    hit, search_status = _find_in_search(pid, identity["title"])

    record: dict[str, Any] = {
        "id": pid,
        "title": identity["title"],
        "url": identity["url"],
        "images": identity["images"],
        "source": "ssr+search",
        "partial": True,
        "detail_note": mtop_note,
    }

    if hit:
        record.update(
            {
                "price": {
                    "sale_price": hit.get("price_formatted"),
                    "sale_value": hit.get("price"),
                    "original_price": hit.get("original_price"),
                    "discount_pct": hit.get("discount_pct"),
                    "currency": hit.get("currency") or CURRENCY,
                },
                "rating": hit.get("rating"),
                "orders": hit.get("orders"),
            }
        )
        unavailable = ["review_count", "stock", "store", "shipping", "variants",
                       "variants_pricing", "specs", "category_path"]
    else:
        record["price"] = None
        record["rating"] = None
        record["orders"] = None
        if search_status == "blocked":
            record["price_note"] = (
                "AliExpress is currently rate-limiting/anti-bot blocking the "
                "search page, which is where this fallback reads price, rating "
                "and orders — so those are unknown for now. This is transient: "
                "the same lookup usually succeeds once the block lifts. Only "
                "the product page's own title and images could be read."
            )
        else:
            record["price_note"] = (
                "searched, but this listing did not appear in the results, so "
                "price, rating and orders are unavailable — only the product "
                "page's own title and images could be read."
            )
        record["search_status"] = search_status
        unavailable = ["price", "rating", "orders", "review_count", "stock",
                       "store", "shipping", "variants", "variants_pricing",
                       "specs", "category_path"]

    record["unavailable"] = unavailable
    return record


def _get_product_via_browser(pid: str) -> Optional[dict]:
    """Full record via a real browser, or ``None`` if it could not be had.

    The browser loads the product page and we intercept the very same
    ``pdp.pc.query`` response the page fetches for itself, so the payload is
    byte-identical to what direct MTop returned before it was gated — and
    ``_extract_product`` parses it unchanged.
    """
    payload = browser.fetch_pdp_payload(pid)
    if not payload:
        return None

    inner = (payload.get("data", {}).get("result", {}) or {})
    gd = (inner.get("GLOBAL_DATA") or {}).get("globalData", {})
    if gd.get("errorCode") == "SITEM_NOT_EXIST":
        raise AliExpressError(f"product {pid} not found (delisted or unavailable)")

    record = _extract_product(payload)
    if record and record.get("title"):
        return record
    log.info("browser returned a pdp payload for %s but it had no title", pid)
    return None


def _get_product(product: str) -> dict:
    """Fetch product detail for a numeric id or AliExpress URL.

    Three transports, cheapest first:

    1. **Direct MTop** — one signed HTTP call. Currently anti-bot gated, but
       still tried (outside its cooldown) so the full record comes back
       automatically the day AliExpress ungates it.
    2. **Browser** — drives the page and intercepts its own MTop response.
       Same full record; costs a page load, so it is not the first choice.
    3. **SSR composite** — product page + search. Partial, and only a last
       resort for when the browser is disabled or cannot run.
    """
    pid = extract_product_id(product)
    if not pid:
        raise AliExpressError(f"could not extract a product id from: {product!r}")

    if _mtop_in_cooldown():
        note = (
            "MTop product detail was anti-bot gated recently, so it was skipped "
            "for this call."
        )
    else:
        mtop_record = _get_product_via_mtop(pid)
        if mtop_record:
            mtop_record["source"] = "mtop"
            mtop_record["partial"] = False
            return mtop_record
        note = (
            "MTop product detail is anti-bot gated (FAIL_SYS_USER_VALIDATE / "
            "RGV587)."
        )

    browser_record = _get_product_via_browser(pid)
    if browser_record:
        browser_record["source"] = "browser"
        browser_record["partial"] = False
        return browser_record

    return _get_product_via_ssr(
        pid,
        note + " The browser transport could not retrieve it either, so the "
        "record below is composed from the product page and search results.",
    )


def liveness() -> dict:
    """Cheap process-level liveness. Does not touch AliExpress (no dependency)."""
    return {"status": "ok", "region": REGION, "currency": CURRENCY, "locale": LOCALE}
