"""Tests for the SSR product-detail fallback (0.2.0).

These are deliberately network-free: the live site is exactly the thing that is
unreliable here, so the parsing and record-assembly logic is pinned against
fixtures that mirror what AliExpress actually served on 2026-08-08. If
AliExpress changes shape, the fallback should fail in *these* tests first.
"""

from __future__ import annotations

import pytest

from aliexpress_mcp import aliexpress_client as ac

PID = "1005006730849854"

# Trimmed to the parts the extractor reads. The og:title suffix, the empty
# runParams and the DCData image list are all reproduced verbatim in shape from
# the real page.
ITEM_PAGE = """<!doctype html><html><head>
<meta name="robots" content="" />
<meta property="og:url" content="//de.aliexpress.com/item/1005006730849854.html" />
<meta property="og:title" content="Original Xiaomi 120W USB Typ-C Kabel 6A Turbo-Schnellladung f&#252;r Mi 17 Pro - AliExpress 202192403" />
<meta property="og:type" content="product" />
<meta property="og:image" content="https://ae-pic-a1.aliexpress-media.com/kf/MAIN.jpg" />
<script>
window.runParams = {
            };
window._d_c_ = window._d_c_ || {};
window._d_c_.viewName = 'newDetail';
window._d_c_.isCSR = true;
window._d_c_.DCData = {"extParams":{"site":"deu"},"name":"ItemDetailResp",
"imagePathList":["https://cdn/a.jpg","https://cdn/b.jpg"],
"summImagePathList":["https://cdn/a_80x80.jpg"]};
</script></head><body></body></html>"""

BLOCKED_PAGE = (
    '<script>sessionStorage.x5referer = window.location.href;'
    'var url = "//de.aliexpress.com/_____tmd_____/punish?x5secdata=abc";</script>'
)

SEARCH_HIT = {
    "id": PID,
    "title": "Original Xiaomi 120W USB Typ-C Kabel",
    "price": 3.29,
    "price_formatted": "3,29 €",
    "currency": "EUR",
    "original_price": 9.99,
    "discount_pct": 67,
    "rating": 4.8,
    "orders": "10.000+ verkauft",
    "image": "https://cdn/a.jpg",
    "url": f"https://www.aliexpress.com/item/{PID}.html",
}


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeSession:
    """Records the urls it was asked for and replays a canned response."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.urls: list[str] = []

    def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        self.urls.append(url)
        return self.response


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """Cooldown is module state; a leaked value would silently skip MTop."""
    ac._mtop_blocked_until = 0.0
    yield
    ac._mtop_blocked_until = 0.0


@pytest.fixture
def item_page(monkeypatch):
    session = FakeSession(FakeResponse(ITEM_PAGE))
    monkeypatch.setattr(ac, "_get_session", lambda: session)
    return session


# --------------------------------------------------------------------------- #
# identity extraction
# --------------------------------------------------------------------------- #


def test_identity_strips_the_aliexpress_suffix_from_og_title(item_page):
    identity = ac._item_page_identity(PID)
    # " - AliExpress 202192403" is a page-title suffix, not part of the listing
    # name, and it would poison the search query built from it.
    assert identity["title"] == (
        "Original Xiaomi 120W USB Typ-C Kabel 6A Turbo-Schnellladung für Mi 17 Pro"
    )


def test_identity_prefers_dcdata_images_over_the_single_og_image(item_page):
    assert ac._item_page_identity(PID)["images"] == [
        "https://cdn/a.jpg",
        "https://cdn/b.jpg",
    ]


def test_identity_falls_back_to_og_image_when_dcdata_is_absent(monkeypatch):
    stripped = ITEM_PAGE.replace("window._d_c_.DCData", "window._d_c_.Other")
    monkeypatch.setattr(ac, "_get_session", lambda: FakeSession(FakeResponse(stripped)))
    assert ac._item_page_identity(PID)["images"] == [
        "https://ae-pic-a1.aliexpress-media.com/kf/MAIN.jpg"
    ]


def test_identity_detects_the_anti_bot_punish_page(monkeypatch):
    monkeypatch.setattr(
        ac, "_get_session", lambda: FakeSession(FakeResponse(BLOCKED_PAGE))
    )
    with pytest.raises(ac.AliExpressError, match="anti-bot"):
        ac._item_page_identity(PID)


def test_identity_raises_when_the_page_has_no_title(monkeypatch):
    monkeypatch.setattr(
        ac, "_get_session", lambda: FakeSession(FakeResponse("<html></html>"))
    )
    with pytest.raises(ac.AliExpressError, match="not found"):
        ac._item_page_identity(PID)


def test_identity_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        ac, "_get_session", lambda: FakeSession(FakeResponse("", status_code=404))
    )
    with pytest.raises(ac.AliExpressError, match="HTTP 404"):
        ac._item_page_identity(PID)


# --------------------------------------------------------------------------- #
# search matching
# --------------------------------------------------------------------------- #


def test_find_in_search_matches_on_id_not_position(monkeypatch):
    other = dict(SEARCH_HIT, id="9999999999")
    monkeypatch.setattr(
        ac, "_search", lambda q, limit: {"items": [other, SEARCH_HIT]}
    )
    hit, status = ac._find_in_search(PID, "some title")
    assert hit["id"] == PID
    assert status == "matched"


def test_find_in_search_retries_with_a_shorter_query(monkeypatch):
    seen: list[str] = []

    def fake_search(query, limit):  # noqa: ANN001
        seen.append(query)
        return {"items": [SEARCH_HIT]} if len(seen) == 2 else {"items": []}

    monkeypatch.setattr(ac, "_search", fake_search)
    title = " ".join(f"w{i}" for i in range(20))
    assert ac._find_in_search(PID, title)[0]["id"] == PID
    # 12-word prefix first, then a 6-word one — and never the full 20 words.
    assert [len(q.split()) for q in seen] == [12, 6]


def test_find_in_search_reports_blocked_separately_from_not_found(monkeypatch):
    """The two must stay distinguishable — they need different responses."""
    def blocked(query, limit):  # noqa: ANN001
        raise ac.AliExpressError("blocked by AliExpress anti-bot (TMD challenge)")

    monkeypatch.setattr(ac, "_search", blocked)
    hit, status = ac._find_in_search(PID, "a title")
    # A blocked search must degrade the record, not abort the whole call...
    assert hit is None
    # ...and must not be reported as "this listing isn't in the results".
    assert status == "blocked"


def test_find_in_search_stops_after_the_first_block(monkeypatch):
    """A blocked search means blocked; retrying just adds load during a punish."""
    calls: list[str] = []

    def blocked(query, limit):  # noqa: ANN001
        calls.append(query)
        raise ac.AliExpressError("blocked by AliExpress anti-bot (TMD challenge)")

    monkeypatch.setattr(ac, "_search", blocked)
    ac._find_in_search(PID, " ".join(f"w{i}" for i in range(20)))
    assert len(calls) == 1


def test_find_in_search_reports_not_found_when_search_worked(monkeypatch):
    monkeypatch.setattr(ac, "_search", lambda q, limit: {"items": []})
    assert ac._find_in_search(PID, "a title") == (None, "not_found")


def test_find_in_search_never_calls_the_gated_public_wrapper(monkeypatch):
    """`_get_product` already holds the semaphore, which is not reentrant."""
    monkeypatch.setattr(ac, "_search", lambda q, limit: {"items": [SEARCH_HIT]})
    monkeypatch.setattr(
        ac, "search", lambda *a, **k: pytest.fail("must not call gated search()")
    )
    assert ac._find_in_search(PID, "title")[0]["id"] == PID


# --------------------------------------------------------------------------- #
# record assembly
# --------------------------------------------------------------------------- #


def test_ssr_record_carries_price_from_search_and_flags_what_is_missing(
    item_page, monkeypatch
):
    monkeypatch.setattr(ac, "_search", lambda q, limit: {"items": [SEARCH_HIT]})
    rec = ac._get_product_via_ssr(PID, "note")

    assert rec["id"] == PID
    assert rec["source"] == "ssr+search"
    assert rec["partial"] is True
    assert rec["price"]["sale_value"] == 3.29
    assert rec["price"]["currency"] == "EUR"
    assert rec["rating"] == 4.8
    assert rec["orders"] == "10.000+ verkauft"
    # Fields MTop alone could supply must be named, never silently dropped.
    assert "specs" in rec["unavailable"]
    assert "variants" in rec["unavailable"]
    assert "store" in rec["unavailable"]
    # ...and the ones we did recover must NOT be listed as unavailable.
    assert "price" not in rec["unavailable"]
    assert "rating" not in rec["unavailable"]


def test_ssr_record_without_a_search_match_says_so_explicitly(item_page, monkeypatch):
    monkeypatch.setattr(ac, "_search", lambda q, limit: {"items": []})
    rec = ac._get_product_via_ssr(PID, "note")

    assert rec["price"] is None
    assert "price_note" in rec
    assert rec["search_status"] == "not_found"
    assert "did not appear in the results" in rec["price_note"]
    assert "price" in rec["unavailable"]
    assert "rating" in rec["unavailable"]
    # Identity still came through, so the answer is not useless.
    assert rec["title"].startswith("Original Xiaomi")
    assert rec["images"]


def test_ssr_record_says_blocked_rather_than_not_found(item_page, monkeypatch):
    """A punish page must not be reported as 'this listing wasn't in results'.

    That wording sends the reader hunting for a matching bug that isn't there —
    the same class of misleading diagnostic as the old "token bootstrap did not
    yield a cookie", which pointed at tokens when the endpoint was gated.
    """
    def blocked(query, limit):  # noqa: ANN001
        raise ac.AliExpressError("blocked by AliExpress anti-bot (TMD challenge)")

    monkeypatch.setattr(ac, "_search", blocked)
    rec = ac._get_product_via_ssr(PID, "note")

    assert rec["search_status"] == "blocked"
    assert "blocking the search page" in rec["price_note"]
    assert "transient" in rec["price_note"]
    assert "did not appear in the results" not in rec["price_note"]


# --------------------------------------------------------------------------- #
# orchestration: MTop preferred, fallback on block, cooldown respected
# --------------------------------------------------------------------------- #


def test_get_product_prefers_mtop_when_it_works(monkeypatch):
    monkeypatch.setattr(
        ac, "_get_product_via_mtop", lambda pid: {"id": pid, "title": "full record"}
    )
    monkeypatch.setattr(
        ac, "_get_product_via_browser", lambda pid: pytest.fail("no browser needed")
    )
    monkeypatch.setattr(
        ac, "_get_product_via_ssr", lambda *a: pytest.fail("should not fall back")
    )
    rec = ac._get_product(PID)
    assert rec["source"] == "mtop"
    assert rec["partial"] is False


def test_get_product_uses_the_browser_when_mtop_is_gated(monkeypatch):
    """The browser is the real answer to the gate — not the partial composite."""
    monkeypatch.setattr(ac, "_get_product_via_mtop", lambda pid: None)
    monkeypatch.setattr(
        ac, "_get_product_via_browser", lambda pid: {"id": pid, "title": "full"}
    )
    monkeypatch.setattr(
        ac, "_get_product_via_ssr", lambda *a: pytest.fail("browser worked; no SSR")
    )
    rec = ac._get_product(PID)
    assert rec["source"] == "browser"
    assert rec["partial"] is False


def test_get_product_falls_back_to_ssr_only_when_the_browser_also_fails(monkeypatch):
    monkeypatch.setattr(ac, "_get_product_via_mtop", lambda pid: None)
    monkeypatch.setattr(ac, "_get_product_via_browser", lambda pid: None)
    monkeypatch.setattr(
        ac, "_get_product_via_ssr", lambda pid, note: {"id": pid, "note": note}
    )
    note = ac._get_product(PID)["note"]
    assert "RGV587" in note
    assert "browser transport could not retrieve it" in note


def test_get_product_still_tries_the_browser_during_mtop_cooldown(monkeypatch):
    """Cooldown suppresses the cheap HTTP attempt, not the working transport."""
    monkeypatch.setattr(
        ac,
        "_get_product_via_mtop",
        lambda pid: pytest.fail("MTop must be skipped during cooldown"),
    )
    monkeypatch.setattr(
        ac, "_get_product_via_browser", lambda pid: {"id": pid, "title": "full"}
    )
    ac._mark_mtop_blocked()
    assert ac._get_product(PID)["source"] == "browser"


def test_browser_path_returns_none_when_no_payload(monkeypatch):
    monkeypatch.setattr(ac.browser, "fetch_pdp_payload", lambda pid: None)
    assert ac._get_product_via_browser(PID) is None


def test_browser_path_parses_the_intercepted_payload(monkeypatch):
    """The payload is the same schema MTop returned, so the parser is reused."""
    payload = {
        "ret": ["SUCCESS::调用成功"],
        "data": {"result": {
            "PRODUCT_TITLE": {"text": "Some Product"},
            "GLOBAL_DATA": {"globalData": {"productId": PID}},
        }},
    }
    monkeypatch.setattr(ac.browser, "fetch_pdp_payload", lambda pid: payload)
    rec = ac._get_product_via_browser(PID)
    assert rec["title"] == "Some Product"
    assert rec["id"] == PID


def test_browser_path_reports_a_delisted_product(monkeypatch):
    payload = {
        "ret": ["SUCCESS::调用成功"],
        "data": {"result": {
            "GLOBAL_DATA": {"globalData": {"errorCode": "SITEM_NOT_EXIST"}}
        }},
    }
    monkeypatch.setattr(ac.browser, "fetch_pdp_payload", lambda pid: payload)
    with pytest.raises(ac.AliExpressError, match="not found"):
        ac._get_product_via_browser(PID)


def test_browser_transport_is_disabled_by_env(monkeypatch):
    monkeypatch.setattr(ac.browser, "BROWSER_ENABLED", False)
    # Must not try to start Chromium when switched off.
    assert ac.browser.fetch_pdp_payload(PID) is None


def test_get_product_rejects_input_with_no_product_id():
    with pytest.raises(ac.AliExpressError, match="could not extract"):
        ac._get_product("not-a-product")


def test_mtop_block_arms_the_cooldown(monkeypatch):
    """An RGV587 answer must set the cooldown, or every call re-pays the block."""
    monkeypatch.setattr(
        ac,
        "_mtop_request",
        lambda api, v, d: {"ret": ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR::SM::x"]},
    )
    assert ac._get_product_via_mtop(PID) is None
    assert ac._mtop_in_cooldown()


def test_mtop_reports_a_delisted_product_rather_than_falling_back(monkeypatch):
    """'Not found' is a real answer; degrading to a partial record would hide it."""
    monkeypatch.setattr(
        ac,
        "_mtop_request",
        lambda api, v, d: {
            "ret": ["SUCCESS::调用成功"],
            "data": {"result": {"GLOBAL_DATA": {"globalData": {"errorCode": "SITEM_NOT_EXIST"}}}},
        },
    )
    with pytest.raises(ac.AliExpressError, match="not found"):
        ac._get_product_via_mtop(PID)
