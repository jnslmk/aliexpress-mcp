"""MCP server exposing AliExpress product search — self-hosted, no API key.

There is no official AliExpress product API, so this mirrors the aliexpress.com
web frontend: server-rendered search pages for ``search_aliexpress`` and the
site's internal MTop API (token bootstrap + MD5 request signing) for
``get_aliexpress_product``. See ``aliexpress_client`` for the gory details and
the fragility caveats.

Ported from Averyy/fetchaller-mcp (MIT). Transport, packaging and ``/healthz``
mirror the sibling ebay-mcp so it runs as a long-lived container behind
LibreChat instead of stdio. Market defaults to Germany / EUR / de_DE.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any, Optional

from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from aliexpress_mcp.aliexpress_client import (
    CURRENCY,
    LOCALE,
    REGION,
    AliExpressError,
    get_product,
    liveness,
    search,
)

log = logging.getLogger("aliexpress-mcp")

mcp = FastMCP(
    name="aliexpress",
    version="0.1.0",
    instructions=(
        "Search AliExpress and inspect product listings. This is key-less and "
        f"scoped to the {REGION} market, so prices are in {CURRENCY} and titles "
        f"are localised to {LOCALE}. Start with `search_aliexpress` to get a list "
        "of products with their ids and prices, then call "
        "`get_aliexpress_product` with a product id (or its full URL) for the full "
        "record: all variants and their prices, rating, images, shipping and the "
        "store. This is read-only — it searches and reads listings, it cannot buy. "
        "AliExpress has no public API; results come from its web frontend and can "
        "occasionally be blocked by anti-bot protection."
    ),
)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


@mcp.tool
def search_aliexpress(
    query: Annotated[
        str,
        Field(description="Search keywords, e.g. 'usb c kabel' or 'anker powerbank'"),
    ],
    limit: Annotated[
        int, Field(description="Maximum results to return (one page holds ~60)", ge=1, le=60)
    ] = 10,
    sort: Annotated[
        Optional[str],
        Field(
            description=(
                "Sort order: 'orders' (best-selling), 'price_asc' (cheapest first), "
                "'price_desc' (most expensive first), 'newest'. Omit for AliExpress's "
                "default relevance ranking."
            )
        ),
    ] = None,
    min_price: Annotated[
        Optional[float],
        Field(description=f"Minimum price filter, in {CURRENCY}."),
    ] = None,
    max_price: Annotated[
        Optional[float],
        Field(description=f"Maximum price filter, in {CURRENCY}."),
    ] = None,
    page: Annotated[
        int, Field(description="Result page (1-indexed), ~60 items per page.", ge=1)
    ] = 1,
) -> dict[str, Any]:
    """Search AliExpress product listings by keyword.

    Returns a list of products — id, title, sale price (and original price /
    discount), star rating, orders-sold text, thumbnail image and product URL.
    Prices and titles follow the configured market (Germany / EUR by default).
    Pass a product's `id` to `get_aliexpress_product` for full details.
    """
    try:
        return search(
            query=query,
            limit=limit,
            sort=sort,
            min_price=min_price,
            max_price=max_price,
            page=page,
        )
    except AliExpressError as exc:
        log.warning("search_aliexpress failed: %s", exc)
        return {"query": query, "returned": 0, "items": [], "error": str(exc)}


@mcp.tool
def get_aliexpress_product(
    product: Annotated[
        str,
        Field(
            description=(
                "AliExpress product id (a numeric string such as '1005009258005772') "
                "or a full product URL like "
                "'https://www.aliexpress.com/item/1005009258005772.html'."
            )
        ),
    ],
) -> dict[str, Any]:
    """Retrieve the full record of one AliExpress product.

    Use after `search_aliexpress` surfaces something worth a closer look. Returns
    title, the selected-variant price plus per-variant pricing, star rating and
    review count, orders sold, stock, the store (name, positive rating, country),
    shipping (origin, ship-to, delivery estimate), the SKU option axes
    (e.g. colour / size), specifications and all product images. Prices are in the
    configured currency (EUR by default).
    """
    try:
        return get_product(product)
    except AliExpressError as exc:
        log.warning("get_aliexpress_product failed: %s", exc)
        return {"query": product, "error": str(exc)}


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Container readiness probe.

    There is no credential or hard dependency to check, so this is always 200 as
    long as the process is up — a blocked AliExpress upstream is a per-request
    condition surfaced in the tool response, not container ill-health.
    """
    return JSONResponse(liveness())


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("aliexpress-mcp starting (market=%s currency=%s locale=%s)", REGION, CURRENCY, LOCALE)

    transport = os.getenv("MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="http",
            host=os.getenv("MCP_HOST", "0.0.0.0"),  # noqa: S104 - containerised
            port=int(os.getenv("MCP_PORT", "8000")),
            path=os.getenv("MCP_PATH", "/mcp"),
        )


if __name__ == "__main__":
    main()
