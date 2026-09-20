"""Tests for the browser retry policy (0.3.x hardening).

Network-free: no Playwright is started. ``_run_guarded`` pins the retry
semantics — transient failures are retried with bounded, jittered exponential
delays, while a clear TMD block stops at once and propagates its reason.
"""

from __future__ import annotations

import pytest

from aliexpress_mcp import browser


@pytest.fixture(autouse=True)
def _no_teardown(monkeypatch):
    monkeypatch.setattr(browser, "_teardown", lambda: None)


def test_run_guarded_stops_immediately_on_a_clear_block(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(browser.time, "sleep", lambda s: sleeps.append(s))
    calls: list[int] = []

    def load() -> None:
        calls.append(1)
        raise browser.BrowserBlocked("search page served an anti-bot punish page")

    # The reason travels with the error — the caller reports the block, not
    # a vague "browser could not retrieve it".
    with pytest.raises(browser.BrowserBlocked, match="punish page"):
        browser._run_guarded("t", load)
    assert len(calls) == 1  # a punish page is a verdict, not a transient miss
    assert sleeps == []  # and retrying into it would only make things worse


def test_run_guarded_retries_transient_failures_then_gives_up(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(browser.time, "sleep", lambda s: sleeps.append(s))
    calls: list[int] = []

    def load() -> None:
        calls.append(1)
        return None

    assert browser._run_guarded("t", load) is None
    assert len(calls) == browser.BROWSER_ATTEMPTS
    # One jittered delay between each pair of attempts, none after the last.
    assert len(sleeps) == browser.BROWSER_ATTEMPTS - 1


def test_retry_delay_is_bounded_and_jittered(monkeypatch):
    monkeypatch.setattr(browser.random, "uniform", lambda a, b: b)  # max jitter
    assert browser._retry_delay(1) == pytest.approx(browser.BROWSER_RETRY_DELAY_S * 1.5)
    # Even far past the base the growth stays capped (8s) before jitter.
    assert browser._retry_delay(50) == pytest.approx(8.0 * 1.5)
    monkeypatch.setattr(browser.random, "uniform", lambda a, b: a)  # min jitter
    assert browser._retry_delay(1) == pytest.approx(browser.BROWSER_RETRY_DELAY_S * 0.5)


def test_context_uses_market_locale_and_configured_browser_dimensions(monkeypatch):
    from aliexpress_mcp import aliexpress_client as ac

    options: dict = {}

    class Context:
        def add_cookies(self, cookies):  # noqa: ANN001
            self.cookies = cookies

        def set_default_timeout(self, timeout):  # noqa: ANN001
            self.timeout = timeout

    class FakeBrowser:
        def new_context(self, **kwargs):  # noqa: ANN003
            options.update(kwargs)
            return Context()

    monkeypatch.setattr(ac, "LOCALE", "en_US")
    monkeypatch.setenv("AE_TIMEZONE", "America/New_York")
    monkeypatch.setenv("AE_VIEWPORT_WIDTH", "1280")
    monkeypatch.setenv("AE_VIEWPORT_HEIGHT", "720")
    monkeypatch.setattr(browser, "_usuc_cookie", lambda: "market-cookie")

    ctx = browser._context(FakeBrowser())
    assert options == {
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "viewport": {"width": 1280, "height": 720},
    }
    assert ctx.cookies[0]["value"] == "market-cookie"


@pytest.mark.parametrize("load", [browser._fetch, browser._fetch_search])
def test_page_http_429_is_browser_blocked(monkeypatch, load):
    class Response:
        status = 429

    class Page:
        url = "https://www.aliexpress.com/"

        def on(self, *_args):  # noqa: ANN002
            pass

        def goto(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return Response()

        def close(self):
            pass

    class Context:
        def new_page(self):
            return Page()

        def close(self):
            pass

    monkeypatch.setattr(browser, "_ensure", lambda: object())
    monkeypatch.setattr(browser, "_context", lambda _browser: Context())
    with pytest.raises(browser.BrowserBlocked, match="HTTP 429"):
        load("1005006730849854" if load is browser._fetch else "https://example.test/")
