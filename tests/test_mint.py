"""Tests.

Unit tests run anywhere. The `live` marker hits the real 1337x and launches a
browser — run with `uv run pytest`, skip with `uv run pytest -m 'not live'`.
"""

import asyncio

import pytest

from minter.browser import Budget
from minter.models import Cookie, MintRequest
from minter.solve import _has_clearance


def test_budget_counts_down():
    b = Budget(10)
    assert b.remaining() <= 10
    assert b.remaining() > 9
    assert not b.expired()


def test_budget_expires():
    b = Budget(0.01)
    asyncio.run(asyncio.sleep(0.05))
    assert b.expired()
    assert b.remaining() == 0


def test_mint_request_rejects_non_http():
    with pytest.raises(ValueError, match="String should match pattern"):
        MintRequest(url="ftp://1337x.to/")


def test_mint_request_clamps_timeout():
    with pytest.raises(ValueError, match="less than or equal to 300"):
        MintRequest(url="https://1337x.to/", timeout=9999)


def test_cookie_ignores_unknown_playwright_fields():
    """Playwright adds fields over time; the model must tolerate being filtered onto."""
    raw = {"name": "cf_clearance", "value": "x", "domain": ".1337x.to", "path": "/", "unknown": 1}
    c = Cookie(**{k: v for k, v in raw.items() if k in Cookie.model_fields})
    assert c.name == "cf_clearance"


class _FakeContext:
    def __init__(self, names):
        self._names = names

    async def cookies(self):
        return [{"name": n} for n in self._names]


async def test_has_clearance_detects_cookie():
    assert await _has_clearance(_FakeContext(["cf_clearance", "other"]))


async def test_has_clearance_false_without_cookie():
    assert not await _has_clearance(_FakeContext(["__cf_bm"]))


@pytest.mark.live
async def test_mint_1337x_returns_clearance():
    """The real thing: mint against the live site."""
    from minter.solve import mint

    res = await mint("https://1337x.to/", timeout=150)

    assert any(c.name == "cf_clearance" for c in res.cookies), "no cf_clearance issued"
    assert res.user_agent, "user_agent must be returned — the cookie is bound to it"
    assert "Firefox" in res.user_agent, f"expected a Firefox UA, got {res.user_agent}"
    assert res.elapsed_ms > 0
