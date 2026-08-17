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
