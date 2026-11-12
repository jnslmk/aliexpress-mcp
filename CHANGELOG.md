# Changelog

## Unreleased

* Replace Patchright/Chromium with checksum-pinned Obscura v0.2.6 stealth over private loopback CDP; retain HTTP-first search and product fallbacks.
* Use AliExpress's native MTop SDK JSON transport with observed request parameters, provider-owned signing/token refresh, market-header forwarding and a shared navigation/request deadline.
* Reap the owned Obscura process group promptly on CLI SIGTERM (HTTP or stdio), before waiting for blocked browser work; scope and restore the CLI signal handler.

## [0.3.3](https://github.com/jnslmk/aliexpress-mcp/compare/v0.3.2...v0.3.3) (2026-09-20)


### Bug Fixes

* **scraper:** close aliexpress transport gaps ([c1b09bb](https://github.com/jnslmk/aliexpress-mcp/commit/c1b09bb5a4262ba8ef235b122cc51a60a98f9d26))
* **scraper:** harden aliexpress transport failures ([9e0c89d](https://github.com/jnslmk/aliexpress-mcp/commit/9e0c89dc9ddb6a5b0aa5e646d08cfcafda680cf9))
* **scraper:** market-aware product urls and classified page timeouts ([ecb1190](https://github.com/jnslmk/aliexpress-mcp/commit/ecb11907525299baf5aeb5d34e2ac9c47c42fa64))

## [0.3.2](https://github.com/jnslmk/aliexpress-mcp/compare/v0.3.1...v0.3.2) (2026-08-24)


### Bug Fixes

* **search:** fall back to the browser transport on an anti-bot block ([5ddc7c5](https://github.com/jnslmk/aliexpress-mcp/commit/5ddc7c59bdcbcb8b19ca00728e331b5a64ca8672))
