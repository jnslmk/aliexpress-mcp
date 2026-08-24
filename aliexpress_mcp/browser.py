"""Browser transport for product detail and search.

AliExpress gates the MTop product-detail endpoints against *non-browser*
clients: `curl_cffi` with Chrome TLS impersonation gets
``FAIL_SYS_USER_VALIDATE / RGV587_ERROR`` and a captcha url, while a real
Chromium on the *same IP*, not logged in, gets ``SUCCESS`` and a ~95 KB
payload. TLS impersonation is not enough — the anti-bot wants the JavaScript
executed. The SSR search page is gated the same way under load: a plain HTTP
client gets a ``_____tmd_____`` punish page in place of the embedded
``_init_data_`` JSON, while a real browser clears the same challenge.

So rather than reimplement that JS, this drives the page and reads the answer
it fetches for itself:

* **Product detail** — load ``/item/<id>.html``, intercept the
  ``mtop.aliexpress.pdp.pc.query`` XHR, hand the JSON to the existing
  ``_extract_product``. That parser is unchanged from the direct-MTop days —
  same endpoint, same schema, different transport.
* **Search** — load the same SSR search URL the plain HTTP path would have
  fetched and return its rendered HTML; ``_extract_init_data`` parses it
  unchanged, since it is the same server-rendered page either way.

Two hard constraints shape the design:

* **Playwright's sync objects are bound to the thread that created them**, and
  FastMCP dispatches sync tools onto an arbitrary worker thread. Everything
  here therefore runs on one dedicated single-worker executor.
* **Browser work must stay serialised.** Chromium is the memory cost of this
  container, and concurrent page loads are also the fastest way back onto
  AliExpress's anti-bot list.

Uses `patchright` — a drop-in Playwright fork with anti-detection patches,
already the house choice in the sibling baumarkt-mcp.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

log = logging.getLogger("aliexpress-mcp")

BROWSER_ENABLED = os.getenv("AE_BROWSER_ENABLED", "true").lower() not in {"0", "false", "no"}
# Wall-clock budget for one product lookup: navigation plus the page's own XHR.
BROWSER_TIMEOUT_MS = int(os.getenv("AE_BROWSER_TIMEOUT_MS", "45000"))
HEADLESS = os.getenv("AE_BROWSER_HEADLESS", "true").lower() not in {"0", "false", "no"}
# AliExpress challenges a proportion of loads; a fresh context usually clears it.
BROWSER_ATTEMPTS = max(1, int(os.getenv("AE_BROWSER_ATTEMPTS", "3")))
BROWSER_RETRY_DELAY_S = float(os.getenv("AE_BROWSER_RETRY_DELAY_S", "1.5"))

_PDP_API = "mtop.aliexpress.pdp.pc.query"

# One worker: it owns the Playwright objects, so every call must land on it.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ae-browser")
_state: dict[str, Any] = {}
_state_lock = threading.Lock()


class BrowserUnavailable(RuntimeError):
    """Raised when the browser transport cannot run at all (not: page failed)."""


def _start() -> tuple[Any, Any]:
    """Start Playwright + Chromium. Runs on the worker thread only."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - depends on the image build
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise BrowserUnavailable(
                "no browser driver installed (expected patchright or playwright)"
            ) from exc

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=HEADLESS,
        args=[
            # Chromium's sandbox needs privileges the container deliberately
            # drops; the process is already confined by the container itself.
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    return pw, browser


def _context(browser: Any) -> Any:
    """A fresh German-market browser context, one per lookup.

    Measured, not assumed: reusing a context across back-to-back lookups gets
    the *second* one answered with ``FAIL_SYS_USER_VALIDATE / RGV587_ERROR``,
    while the first request of a fresh context succeeds. The intuition that a
    warm, already-checked context would be challenged *less* is exactly
    backwards here — carried-over state is what marks it. Only the Chromium
    process is reused; contexts are cheap and disposable.
    """
    ctx = browser.new_context(
        locale="de-DE",
        timezone_id="Europe/Berlin",
        viewport={"width": 1440, "height": 900},
    )
    ctx.add_cookies([{
        "name": "aep_usuc_f",
        "value": os.getenv(
            "AE_USUC_COOKIE", "site=glo&c_tp=EUR&region=DE&b_locale=de_DE"
        ),
        "domain": ".aliexpress.com",
        "path": "/",
    }])
    ctx.set_default_timeout(BROWSER_TIMEOUT_MS)
    return ctx


def _ensure() -> Any:
    """Lazily start Chromium; return the browser. Worker thread only."""
    if "browser" in _state:
        return _state["browser"]
    pw, browser_obj = _start()
    _state["pw"], _state["browser"] = pw, browser_obj
    log.info("browser transport started (headless=%s)", HEADLESS)
    return browser_obj


def _teardown() -> None:
    """Drop everything so the next call starts clean. Worker thread only."""
    for key in ("browser", "pw"):
        obj = _state.pop(key, None)
        if obj is None:
            continue
        try:
            obj.close() if key != "pw" else obj.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the real error
            pass


def _fetch(pid: str) -> Optional[dict]:
    """Load the product page and return its own pdp.pc.query JSON. Worker only."""
    ctx = _context(_ensure())
    page = ctx.new_page()
    captured: dict[str, Any] = {}

    def on_response(resp: Any) -> None:
        if _PDP_API not in resp.url or "payload" in captured:
            return
        try:
            body = resp.text()
        except Exception:  # noqa: BLE001 - a body we cannot read is not a payload
            return
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end <= start:
            return
        try:
            obj = json.loads(body[start:end + 1])
        except json.JSONDecodeError:
            return
        # The page's first pdp call is the unsigned token bootstrap and comes
        # back FAIL_SYS_TOKEN_EMPTY; the signed retry carries the data. Waiting
        # for SUCCESS rather than for "a response" is what makes that a
        # non-issue instead of an intermittent empty result.
        ret = " ".join(obj.get("ret") or [])
        if "SUCCESS" in ret:
            captured["payload"] = obj
        else:
            # Logged, because "the browser was challenged" and "the browser
            # never got a response" are different faults with the same symptom
            # (an empty result after the full timeout).
            captured.setdefault("rets", []).append(ret[:60])

    page.on("response", on_response)
    try:
        page.goto(
            f"https://de.aliexpress.com/item/{pid}.html",
            wait_until="domcontentloaded",
            timeout=BROWSER_TIMEOUT_MS,
        )
        # Poll via page.wait_for_timeout rather than a threading.Event: the sync
        # Playwright API only dispatches "response" events while control is
        # inside a Playwright call, so blocking on an Event would park this
        # thread with the handler never firing — the page loads, the payload
        # arrives, and we time out having seen nothing.
        waited = 0
        step = 250
        while "payload" not in captured and waited < BROWSER_TIMEOUT_MS:
            page.wait_for_timeout(step)
            waited += step
            # Once the page has been told "RGV587" there is nothing left to
            # wait for — it will not retry itself. Bail out at once and let the
            # caller open a fresh context rather than sit out the full timeout;
            # this is the difference between a ~2s retry and a 45s dead wait.
            if any("RGV587" in r for r in captured.get("rets", [])):
                break
        payload = captured.get("payload")
        if payload is None:
            log.info(
                "browser saw no successful %s for %s in %sms (pdp replies: %s; "
                "page url: %s)",
                _PDP_API, pid, waited,
                captured.get("rets") or "none", page.url[:120],
            )
        return payload
    finally:
        for obj in (page, ctx):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass


def _run_guarded(label: str, load: Any) -> Optional[Any]:
    """Retry ``load()`` in a fresh context on failure. Worker thread only.

    AliExpress challenges a *proportion* of page loads rather than locking on:
    an attempt that gets RGV587 (or a search punish page) is routinely
    followed by one that succeeds in ~2s. Retrying is therefore worth far more
    than it costs — but it is capped, because hammering a site that just said
    no is how the whole IP gets a multi-hour punish.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, BROWSER_ATTEMPTS + 1):
        try:
            result = load()
            if result:
                if attempt > 1:
                    log.info("%s succeeded on attempt %s", label, attempt)
                return result
        except Exception as exc:  # noqa: BLE001 - a bad page must not wedge the browser
            last_exc = exc
            log.info("%s errored (%s); restarting browser", label, exc)
            _teardown()
        if attempt < BROWSER_ATTEMPTS:
            # A short, deliberate pause: retrying instantly is what the anti-bot
            # is watching for.
            time.sleep(BROWSER_RETRY_DELAY_S)
    if last_exc is None:
        log.info("%s gave up after %s attempts", label, BROWSER_ATTEMPTS)
    return None


def fetch_pdp_payload(pid: str) -> Optional[dict]:
    """Return the product's own ``pdp.pc.query`` JSON, or ``None``.

    Thread-safe entry point: hops onto the dedicated worker and serialises
    callers behind it.
    """
    if not BROWSER_ENABLED:
        return None
    with _state_lock:
        future = _executor.submit(_run_guarded, f"browser lookup for {pid}", lambda: _fetch(pid))
    return future.result(timeout=(BROWSER_TIMEOUT_MS / 1000.0) + 60)


_TMD_MARKERS = ("_____tmd_____", "x5secdata")


def _fetch_search(url: str) -> Optional[str]:
    """Load the SSR search page and return its HTML, or ``None`` if challenged.

    Worker thread only. Unlike product detail, nothing needs intercepting:
    the search page is server-rendered, so the HTML the navigation itself
    receives already contains (or, when challenged, withholds) the
    ``_init_data_`` blob ``_extract_init_data`` parses.
    """
    ctx = _context(_ensure())
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        html = page.content()
        if any(marker in html for marker in _TMD_MARKERS):
            return None
        return html
    finally:
        for obj in (page, ctx):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass


def fetch_search_html(url: str) -> Optional[str]:
    """Return the SSR search page's HTML via a real browser, or ``None``.

    Thread-safe entry point: hops onto the dedicated worker and serialises
    callers behind it.
    """
    if not BROWSER_ENABLED:
        return None
    with _state_lock:
        future = _executor.submit(_run_guarded, f"browser search load for {url}", lambda: _fetch_search(url))
    return future.result(timeout=(BROWSER_TIMEOUT_MS / 1000.0) + 60)


def shutdown() -> None:
    """Best-effort browser teardown (tests, or a clean process exit)."""
    with _state_lock:
        try:
            _executor.submit(_teardown).result(timeout=30)
        except Exception:  # noqa: BLE001
            pass
