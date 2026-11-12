"""Serial browser transport using a private, worker-owned Obscura CDP service.

Search remains HTTP-first. Product detail loads the real market page, then
asks its native MTop SDK for JSON using the page's observed request parameters.
The provider SDK alone owns signing and token refresh; no credentials are replayed.
Every lookup gets a fresh market context on the existing single-worker executor.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import signal
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

log = logging.getLogger("aliexpress-mcp")

BROWSER_ENABLED = os.getenv("AE_BROWSER_ENABLED", "true").lower() not in {"0", "false", "no"}
# Wall-clock budget for one product lookup: navigation plus the page's own XHR.
BROWSER_TIMEOUT_MS = int(os.getenv("AE_BROWSER_TIMEOUT_MS", "45000"))
# AliExpress challenges a proportion of loads; a fresh context usually clears it.
BROWSER_ATTEMPTS = max(1, int(os.getenv("AE_BROWSER_ATTEMPTS", "3")))
BROWSER_RETRY_DELAY_S = float(os.getenv("AE_BROWSER_RETRY_DELAY_S", "1.5"))

_PDP_API = "mtop.aliexpress.pdp.pc.query"

# One worker: it owns the Playwright objects, so every call must land on it.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ae-browser")
_state: dict[str, Any] = {}
_state_lock = threading.Lock()
_shutting_down = threading.Event()


class BrowserUnavailable(RuntimeError):
    """Raised when the browser transport cannot run at all (not: page failed)."""


class BrowserBlocked(RuntimeError):
    """AliExpress served a clear anti-bot block (TMD punish page).

    Unlike a transient challenge this is an IP-level verdict: retrying earns a
    longer punish. The reason travels with the error into the caller's note.
    """


def _stop_service() -> None:
    """Reap the CLI and all workers, including children of an exited CLI."""
    process = _state.pop("service", None)
    if process is None:
        return
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _start() -> tuple[Any, Any]:
    """Start loopback-only Obscura and connect ordinary Playwright over CDP."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the image build
        raise BrowserUnavailable("Playwright is not installed") from exc

    pw = None
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = os.environ.copy()
        env["OBSCURA_NAV_TIMEOUT_MS"] = str(BROWSER_TIMEOUT_MS)
        # Do not forward provider console output: it can contain tokens/URLs.
        process = subprocess.Popen(
            [os.getenv("AE_OBSCURA_BINARY", "/opt/obscura/obscura"),
             "serve", "--host", "127.0.0.1", "--port", str(port),
             "--max-connections", "6", "--stealth"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _state["service"] = process
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        opener = build_opener(ProxyHandler({}))
        while True:
            if _shutting_down.is_set():
                raise BrowserUnavailable("Obscura is shutting down")
            if process.poll() is not None:
                raise BrowserUnavailable(f"Obscura exited during startup (status {process.returncode})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserUnavailable("Obscura CDP startup exceeded 30 seconds")
            try:
                with opener.open(f"{endpoint}/json/version", timeout=min(1, remaining)) as response:
                    ready = bool(json.load(response).get("webSocketDebuggerUrl"))
                if ready:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(min(0.1, remaining))
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(
            endpoint, timeout=max(1, int((deadline - time.monotonic()) * 1000)),
        )
        return pw, browser
    except Exception as exc:
        _stop_service()
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001 - preserve the startup failure
                pass
        if isinstance(exc, BrowserUnavailable):
            raise
        raise BrowserUnavailable(f"Obscura startup failed ({type(exc).__name__})") from None


def _context(browser: Any) -> Any:
    """A fresh market-context browser context, one per lookup.

    Locale follows the client's existing ``AE_LOCALE`` setting. Timezone and
    viewport keep their current values by default, but are configurable so a
    non-German market does not have to impersonate Berlin on a 1440x900 screen.
    Measured, not assumed: reusing a context across back-to-back lookups gets
    the *second* one answered ``FAIL_SYS_USER_VALIDATE / RGV587_ERROR``, while
    the first request of a fresh context succeeds.
    """
    from aliexpress_mcp import aliexpress_client

    ctx = browser.new_context(
        locale=aliexpress_client.LOCALE.replace("_", "-"),
        timezone_id=os.getenv("AE_TIMEZONE", "Europe/Berlin"),
        viewport={
            "width": int(os.getenv("AE_VIEWPORT_WIDTH", "1440")),
            "height": int(os.getenv("AE_VIEWPORT_HEIGHT", "900")),
        },
    )
    ctx.add_cookies([{
        "name": "aep_usuc_f",
        # Same cookie the direct HTTP transport sends (aliexpress_client owns
        # it, AE_USUC_COOKIE overrides it for both), so the two transports
        # always agree on region / currency / locale. Late import: the client
        # imports this module, so a module-level import would be circular.
        "value": _usuc_cookie(),
        "domain": ".aliexpress.com",
        "path": "/",
    }])
    locale = aliexpress_client.LOCALE.replace("_", "-")
    language = locale.split("-", 1)[0]
    accept_language = f"{locale},{language};q=0.9" if locale != language else language
    # CDP extra headers do not reach Obscura's stealth HTTP client; routing does.
    ctx.route("**/*", lambda route: route.continue_(
        headers=dict(route.request.headers) | {"accept-language": accept_language},
    ))
    ctx.set_default_timeout(BROWSER_TIMEOUT_MS)
    return ctx


def _usuc_cookie() -> str:
    """The shared market cookie; lives in aliexpress_client (market config)."""
    from aliexpress_mcp import aliexpress_client

    return aliexpress_client.USUC_COOKIE


def _ensure() -> Any:
    """Lazily start Obscura; return the browser. Worker thread only."""
    if _shutting_down.is_set():
        raise BrowserUnavailable("Obscura is shutting down")
    if "browser" in _state:
        if _state["browser"].is_connected() and _state["service"].poll() is None:
            return _state["browser"]
        _teardown()
    pw, browser_obj = _start()
    _state["pw"], _state["browser"] = pw, browser_obj
    log.info("browser transport started (Obscura stealth, loopback CDP)")
    return browser_obj


def _teardown() -> None:
    """Drop everything so the next call starts clean. Worker thread only."""
    _stop_service()
    for key in ("browser", "pw"):
        obj = _state.pop(key, None)
        if obj is None:
            continue
        try:
            obj.close() if key != "pw" else obj.stop()
        except Exception:  # noqa: BLE001 - teardown must not mask the real error
            pass


def _fetch(pid: str) -> Optional[dict]:
    """Return successful native-SDK PDP JSON within one attempt's budget."""
    from aliexpress_mcp import aliexpress_client

    ctx = _context(_ensure())
    page = None
    deadline = time.monotonic() + BROWSER_TIMEOUT_MS / 1000
    captured: dict[str, Any] = {}

    def on_response(resp: Any) -> None:
        target = urlsplit(resp.url)
        if (target.scheme != "https" or target.hostname != "acs.aliexpress.com"
                or target.path.lower() != f"/h5/{_PDP_API}/1.0/"):
            return
        captured.setdefault("url", resp.url)
        if _response_status(resp) == 429:
            captured["rate_limited"] = True

    try:
        page = ctx.new_page()
        page.on("response", on_response)
        lang = aliexpress_client.LOCALE.split("_")[0].lower()
        host = "www" if lang == "en" else lang
        response = page.goto(
            f"https://{host}.aliexpress.com/item/{pid}.html",
            wait_until="domcontentloaded",
            timeout=max(1, int((deadline - time.monotonic()) * 1000)),
        )
        if _response_status(response) == 429:
            raise BrowserBlocked("product page returned HTTP 429 (rate limited)")
        while "url" not in captured and time.monotonic() < deadline:
            if _is_punish_url(page.url):
                raise BrowserBlocked("product page redirected to an anti-bot punish page")
            page.wait_for_timeout(min(250, max(1, (deadline - time.monotonic()) * 1000)))
        if _is_punish_url(page.url):
            raise BrowserBlocked("product page redirected to an anti-bot punish page")
        if captured.get("rate_limited"):
            raise BrowserBlocked("product API returned HTTP 429 (rate limited)")
        remaining = int((deadline - time.monotonic()) * 1000)
        if "url" not in captured or remaining <= 0:
            return None
        answer = page.evaluate("""async ({url, budget}) => {
            const sdk = window.lib && window.lib.mtop;
            if (!sdk || typeof sdk.request !== 'function') return null;
            const params = new URL(url).searchParams;
            if (!['api', 'v', 'appKey', 'data'].every(key => params.has(key))) return null;
            let timer;
            try {
                const options = {api: params.get('api'), v: params.get('v'),
                    appKey: params.get('appKey'), data: JSON.parse(params.get('data')),
                    type: 'get', dataType: 'json', H5Request: true,
                    timeout: Math.min(10000, budget)};
                return await Promise.race([
                    Promise.resolve(sdk.request(options)).catch(payload =>
                        payload && typeof payload === 'object' && 'ret' in payload ? payload : null),
                    new Promise(resolve => { timer = setTimeout(() => resolve(null), budget); })
                ]);
            } catch (_) { return null; }
            finally { clearTimeout(timer); }
        }""", {"url": captured["url"], "budget": remaining})
        if captured.get("rate_limited"):
            raise BrowserBlocked("product API returned HTTP 429 (rate limited)")
        if _is_punish_url(page.url):
            raise BrowserBlocked("product page redirected to an anti-bot punish page")
        # A rejected SDK promise can carry a real MTop error object. Never hand
        # its data to the parser as if a completed request meant SUCCESS.
        ret = answer.get("ret") if isinstance(answer, dict) else None
        if isinstance(ret, list) and any(
            isinstance(code, str) and (code == "SUCCESS" or code.startswith("SUCCESS::"))
            for code in ret
        ):
            return answer
        return None
    finally:
        for obj in (page, ctx):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001 - preserve the lookup error
                    pass


def _retry_delay(attempt: int) -> float:
    """Bounded exponential backoff with jitter, seeded by the configured base."""
    return min(BROWSER_RETRY_DELAY_S * 2 ** (attempt - 1), 8.0) * random.uniform(0.5, 1.5)


def _run_guarded(label: str, load: Any) -> Optional[Any]:
    """Retry ``load()`` in a fresh context on failure. Worker thread only.

    AliExpress challenges a *proportion* of page loads rather than locking on:
    an attempt that gets RGV587 (or a search punish page) is routinely
    followed by one that succeeds. Transient failures are therefore retried
    with bounded, jittered exponential delays — capped, because hammering a
    site that just said no is how the whole IP gets a multi-hour punish. A
    clear TMD punish page is different: it stops the loop at once and raises
    ``BrowserBlocked`` with the reason, so the caller can report the block
    instead of a generic "browser could not retrieve it".
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, BROWSER_ATTEMPTS + 1):
        if _shutting_down.is_set():
            return None
        try:
            result = load()
            if result:
                if attempt > 1:
                    log.info("%s succeeded on attempt %s", label, attempt)
                return result
        except BrowserBlocked as exc:
            log.warning("%s was blocked by AliExpress anti-bot: %s", label, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - a bad page must not wedge the browser
            last_exc = exc
            log.info("%s errored (%s); restarting browser", label, type(exc).__name__)
            if isinstance(exc, BrowserUnavailable):
                log.warning("browser unavailable: %s", exc)
            _teardown()
        if attempt < BROWSER_ATTEMPTS:
            time.sleep(_retry_delay(attempt))
    if last_exc is None:
        log.info("%s gave up after %s attempts", label, BROWSER_ATTEMPTS)
    return None


def _future_result(future: Any, timeout: float) -> Optional[Any]:
    """Collect a worker future, classifying a wall-clock timeout as a block.

    ``_run_guarded``'s worst case (attempts × goto + poll + retry sleeps) can
    outlast the wait budget here, and callers only catch ``BrowserBlocked`` —
    a bare ``TimeoutError`` would escape every classified-error layer.
    """
    try:
        return future.result(timeout=timeout)
    except TimeoutError as exc:
        raise BrowserBlocked(
            "browser page load outlasted its wall-clock budget "
            "(persistently slow or silently challenged page)"
        ) from exc


def fetch_pdp_payload(pid: str) -> Optional[dict]:
    """Return the product's own ``pdp.pc.query`` JSON, or ``None``.

    Thread-safe entry point: hops onto the dedicated worker and serialises
    callers behind it.
    """
    if not BROWSER_ENABLED:
        return None
    with _state_lock:
        future = _executor.submit(_run_guarded, f"browser lookup for {pid}", lambda: _fetch(pid))
    return _future_result(future, (BROWSER_TIMEOUT_MS / 1000.0) + 60)


_TMD_MARKERS = ("_____tmd_____", "x5secdata")


def _is_punish_url(url: str) -> bool:
    """True when the page landed on an anti-bot punish/verification URL."""
    return "_____tmd_____" in url or "punish" in url.lower()


def _response_status(response: Any) -> Optional[int]:
    """Playwright response status; ``None`` when goto returned nothing."""
    return getattr(response, "status", None)


def _fetch_search(url: str) -> Optional[str]:
    """Load the SSR search page and return its HTML.

    Worker thread only. Unlike product detail, nothing needs intercepting:
    the search page is server-rendered, so the HTML the navigation itself
    receives already contains (or, when challenged, withholds) the
    ``_init_data_`` blob ``_extract_init_data`` parses. A clear punish page
    raises ``BrowserBlocked`` rather than retrying.
    """
    ctx = _context(_ensure())
    page = ctx.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        if _response_status(response) == 429:
            raise BrowserBlocked("search page returned HTTP 429 (rate limited)")
        html = page.content()
        if any(marker in html for marker in _TMD_MARKERS) or _is_punish_url(page.url):
            raise BrowserBlocked("search page served an anti-bot punish page")
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
    return _future_result(future, (BROWSER_TIMEOUT_MS / 1000.0) + 60)


def shutdown() -> None:
    """Stop accepting browser work and reap the service on clean process exit."""
    _shutting_down.set()
    # Kill the owned group before queueing behind an in-flight Playwright call.
    # Waiting for that call first can outlast a service manager's TERM grace.
    _stop_service()
    with _state_lock:
        try:
            _executor.submit(_teardown).result(timeout=30)
        except Exception:  # noqa: BLE001 - still kill the service if worker is stuck
            pass
        finally:
            _stop_service()


# The executor is already joined when atexit runs; only process cleanup belongs here.
atexit.register(_stop_service)
