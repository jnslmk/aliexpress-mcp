"""Tests for the `limit`/`max_results` alias resolution on `search_aliexpress`.

FastMCP emits ``additionalProperties: false`` per tool, and across the sibling
product-search MCP servers the result-cap knob is spelled two different ways
(`max_results` in geizhals-mcp/baumarkt-mcp, `limit` here). A model that
carries the wrong name over gets a schema rejection that names no field, so
`search_aliexpress` accepts both — this pins the resolution logic itself,
independent of the network-touching `search()` call it feeds.
"""

from __future__ import annotations

import pytest

from aliexpress_mcp import aliexpress_client as ac
from aliexpress_mcp import server


def test_resolve_limit_uses_the_native_name():
    assert server._resolve_limit(5, None) == 5


def test_resolve_limit_accepts_the_alias():
    assert server._resolve_limit(None, 5) == 5


def test_resolve_limit_defaults_when_neither_is_supplied():
    assert server._resolve_limit(None, None) == 10


def test_resolve_limit_accepts_agreeing_duplicates():
    assert server._resolve_limit(5, 5) == 5


def test_resolve_limit_accepts_agreeing_duplicates_across_str_and_int():
    # LLMs routinely send numeric strings; agreement must survive the coercion.
    assert server._resolve_limit("5", 5) == 5


def test_resolve_limit_rejects_disagreeing_values():
    with pytest.raises(ValueError, match="limit and max_results"):
        server._resolve_limit(5, 20)


def test_resolve_limit_clamps_to_the_existing_cap():
    assert server._resolve_limit(None, 999) == 60


# --------------------------------------------------------------------------- #
# fail-loud tool boundary
# --------------------------------------------------------------------------- #
# FastMCP turns a raised exception into an MCP tool error; the old behaviour
# (catching AliExpressError and returning an empty/success-shaped dict) made a
# block or a broken parser indistinguishable from "no hits".


def test_search_tool_propagates_aliexpress_errors(monkeypatch):
    def blocked(**kwargs):  # noqa: ANN003
        raise ac.AliExpressError("blocked by AliExpress anti-bot (TMD challenge)")

    monkeypatch.setattr(server, "search", blocked)
    with pytest.raises(ac.AliExpressError, match="blocked by AliExpress"):
        server.search_aliexpress(query="usb kabel")


def test_product_tool_propagates_aliexpress_errors(monkeypatch):
    def missing(product):  # noqa: ANN001
        raise ac.AliExpressError("product 1 not found (delisted or unavailable)")

    monkeypatch.setattr(server, "get_product", missing)
    with pytest.raises(ac.AliExpressError, match="not found"):
        server.get_aliexpress_product(product="1")


def test_search_tool_still_returns_a_genuinely_empty_result(monkeypatch):
    # An empty result must now mean exactly one thing: the client found no hits.
    monkeypatch.setattr(server, "search", lambda **kwargs: {"returned": 0, "items": []})
    assert server.search_aliexpress(query="usb kabel") == {"returned": 0, "items": []}
