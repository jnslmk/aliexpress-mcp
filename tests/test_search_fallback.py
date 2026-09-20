"""Tests for the search browser fallback.

Search tries the cheap plain-HTTP call first, same as before. What's new is
what happens when that call comes back with a TMD punish page instead of the
``_init_data_`` blob: it now retries the identical URL through the browser
transport, mirroring the transport chain product detail already had. These
tests are network-free: both the HTTP session and the browser are faked.
"""

from __future__ import annotations

import json

import pytest

from aliexpress_mcp import aliexpress_client as ac

ITEM = {
    "productId": "1005006730849854",
    "title": {"displayTitle": "Original Xiaomi 120W USB Typ-C Kabel"},
    "prices": {
        "salePrice": {"minPrice": 3.29, "formattedPrice": "3,29 €", "currencyCode": "EUR"},
        "originalPrice": {"minPrice": 9.99},
    },
    "evaluation": {"starRating": 4.8},
    "trade": {"tradeDesc": "10.000+ verkauft"},
    "image": {"imgUrl": "//cdn/a.jpg"},
}

_INIT_DATA_OBJ = {
    "data": {
        "root": {
            "fields": {
                "mods": {"itemList": {"content": [ITEM]}},
                "pageInfo": {"totalResults": 1, "page": 1},
            }
        }
    }
}

SEARCH_PAGE = (
    "<!-- init-data-start -->"
    f"<script>window._dida_config_._init_data_ = {{data: {json.dumps(_INIT_DATA_OBJ)}}};</script>"
    "<!-- init-data-end -->"
)

BLOCKED_PAGE = (
    '<script>sessionStorage.x5referer = window.location.href;'
    'var url = "//de.aliexpress.com/_____tmd_____/punish?x5secdata=abc";</script>'
)


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, headers: dict | None = None) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        return self.response


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """Real pacing/backoff sleeps would slow every network-free test here."""
    monkeypatch.setattr(ac, "_pace", lambda: None)
    monkeypatch.setattr(ac, "_backoff_sleep", lambda *a, **k: None)


def test_search_parses_normally_without_touching_the_browser(monkeypatch):
    monkeypatch.setattr(ac, "_get_session", lambda: FakeSession(FakeResponse(SEARCH_PAGE)))
    monkeypatch.setattr(
        ac.browser, "fetch_search_html", lambda url: pytest.fail("no fallback needed")
    )
    result = ac._search("xiaomi cable")
    assert result["items"][0]["id"] == "1005006730849854"


def test_search_falls_back_to_the_browser_on_a_tmd_block(monkeypatch):
    monkeypatch.setattr(ac, "_get_session", lambda: FakeSession(FakeResponse(BLOCKED_PAGE)))
    monkeypatch.setattr(ac.browser, "fetch_search_html", lambda url: SEARCH_PAGE)
    result = ac._search("xiaomi cable")
    assert result["items"][0]["id"] == "1005006730849854"


def test_search_browser_fallback_gets_the_same_url_as_the_blocked_request(monkeypatch):
    monkeypatch.setattr(ac, "_get_session", lambda: FakeSession(FakeResponse(BLOCKED_PAGE)))
    seen: list[str] = []

    def fake_fetch(url):  # noqa: ANN001
        seen.append(url)
        return SEARCH_PAGE

    monkeypatch.setattr(ac.browser, "fetch_search_html", fake_fetch)
    ac._search("xiaomi cable", page=2)
    assert seen and "wholesale-xiaomi-cable.html" in seen[0]
    assert "page=2" in seen[0]


def test_search_raises_when_the_browser_fallback_also_fails(monkeypatch):
    monkeypatch.setattr(ac, "_get_session", lambda: FakeSession(FakeResponse(BLOCKED_PAGE)))
    monkeypatch.setattr(ac.browser, "fetch_search_html", lambda url: None)
    with pytest.raises(ac.AliExpressError, match="browser fallback could not clear"):
        ac._search("xiaomi cable")


def test_search_reports_the_browser_block_reason(monkeypatch):
    monkeypatch.setattr(ac, "_get_session", lambda: FakeSession(FakeResponse(BLOCKED_PAGE)))

    def blocked(_url):  # noqa: ANN001
        raise ac.browser.BrowserBlocked("HTTP 429 (rate limited)")

    monkeypatch.setattr(ac.browser, "fetch_search_html", blocked)
    with pytest.raises(ac.AliExpressError, match="browser search transport failed: BrowserBlocked: HTTP 429"):
        ac._search("xiaomi cable")


def test_search_does_not_treat_a_parser_miss_as_a_block(monkeypatch):
    """Missing/changed JSON shape must not silently try the browser and hide the real error."""
    monkeypatch.setattr(ac, "_get_session", lambda: FakeSession(FakeResponse("<html></html>")))
    monkeypatch.setattr(
        ac.browser, "fetch_search_html", lambda url: pytest.fail("not a block, no fallback")
    )
    with pytest.raises(ac.AliExpressError, match="could not locate product data"):
        ac._search("xiaomi cable")


# --------------------------------------------------------------------------- #
# hardening: HTTP status handling, dedup, malformed fields
# --------------------------------------------------------------------------- #


def test_search_429_backs_off_honoring_retry_after_then_falls_back(monkeypatch):
    sleeps: list[tuple] = []
    monkeypatch.setattr(
        ac,
        "_get_session",
        lambda: FakeSession(
            FakeResponse("rate limited", status_code=429, headers={"Retry-After": "7"})
        ),
    )
    monkeypatch.setattr(
        ac, "_backoff_sleep", lambda attempt, retry_after=None: sleeps.append((attempt, retry_after))
    )
    monkeypatch.setattr(ac.browser, "fetch_search_html", lambda url: SEARCH_PAGE)
    result = ac._search("xiaomi cable")
    assert result["items"][0]["id"] == "1005006730849854"
    assert sleeps == [(1, 7.0)]


def test_search_non_retryable_status_raises_without_browser_fallback(monkeypatch):
    monkeypatch.setattr(
        ac, "_get_session", lambda: FakeSession(FakeResponse("", status_code=403))
    )
    monkeypatch.setattr(
        ac.browser, "fetch_search_html", lambda url: pytest.fail("403 must not fall back")
    )
    with pytest.raises(ac.AliExpressHTTPError, match="search page returned HTTP 403"):
        ac._search("xiaomi cable")


def test_search_reports_which_signal_blocked_it(monkeypatch):
    monkeypatch.setattr(
        ac,
        "_get_session",
        lambda: FakeSession(
            FakeResponse(BLOCKED_PAGE, status_code=429, headers={"Retry-After": "2"})
        ),
    )
    monkeypatch.setattr(ac.browser, "fetch_search_html", lambda url: None)
    with pytest.raises(ac.AliExpressError, match=r"anti-bot \(HTTP 429\)"):
        ac._search("xiaomi cable")


def test_dedupe_keeps_first_occurrence_and_passes_idless_items_through():
    items = [
        {"id": "1", "t": "a"},
        {"id": "", "t": "x"},
        {"id": "1", "t": "dup"},
        {"id": "2", "t": "c"},
    ]
    assert ac._dedupe_by_id(items) == [{"id": "1", "t": "a"}, {"id": "", "t": "x"}, {"id": "2", "t": "c"}]


def test_search_result_is_deduplicated_by_product_id(monkeypatch):
    dup = json.loads(json.dumps(ITEM))
    dup["title"] = {"displayTitle": "same listing, second slot"}
    page = SEARCH_PAGE.replace(
        json.dumps([ITEM]),
        json.dumps([ITEM, dup, ITEM]),
    )
    monkeypatch.setattr(ac, "_get_session", lambda: FakeSession(FakeResponse(page)))
    result = ac._search("xiaomi cable")
    assert [i["title"] for i in result["items"]] == [
        "Original Xiaomi 120W USB Typ-C Kabel"
    ]
    assert result["returned"] == 1


def test_parse_search_item_names_a_malformed_field():
    with pytest.raises(ac.AliExpressError, match="'prices'"):
        ac._parse_search_item({"productId": "1", "prices": "3,29 €"})
    with pytest.raises(ac.AliExpressError, match="'evaluation'"):
        ac._parse_search_item({"productId": "1", "evaluation": 4.8})
