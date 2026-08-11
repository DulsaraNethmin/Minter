"""Browser lifecycle.

One browser process at a time, process-wide. Concurrent mint requests queue on the
lock rather than launching parallel Firefox instances — on a Pi, three at once means
swapping.
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import NamedTuple, cast

from invisible_playwright.async_api import InvisiblePlaywright
from playwright.async_api import Browser, BrowserContext, Page, Route
from playwright_captcha import ClickSolver, FrameworkType

from minter.config import (
    BLOCK_MEDIA,
    BROWSER_LOCALE,
    MAX_ATTEMPTS,
    configure_logging,
    proxy_config,
)

logger = configure_logging()

# Serialises every mint. Held for the whole browser lifetime, not just the launch,
# so a slow challenge cannot overlap with a second launch.
_MINT_LOCK = asyncio.Lock()

_BLOCKED_RESOURCES = {"image", "media", "font"}


class Budget:
    """Wall-clock allowance for a single mint."""

    def __init__(self, seconds: float) -> None:
        self.total = seconds
        self._start = time.perf_counter()

    def remaining(self) -> float:
        return max(0.0, self.total - (time.perf_counter() - self._start))

    def remaining_ms(self) -> float:
        return self.remaining() * 1000

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)

    def expired(self) -> bool:
        return self.remaining() <= 0


class Session(NamedTuple):
    page: Page
    context: BrowserContext


@asynccontextmanager
async def click_solver(page: Page) -> AsyncGenerator[ClickSolver]:
    """Construct a ClickSolver only when one is actually needed.

    Entering ClickSolver installs handlers on the page, and measurement showed that
    doing so eagerly interferes with the non-interactive interstitial: with the solver
    attached the challenge stopped clearing on its own and every request degraded into
    five failed click attempts. Since this challenge type has no widget to click, the
    solver is now built lazily and only as a fallback.
    """
    async with ClickSolver(
        framework=FrameworkType.PLAYWRIGHT,
        page=page,
        max_attempts=MAX_ATTEMPTS,
        attempt_delay=1,
    ) as solver:
        yield solver


async def _block_media(route: Route) -> None:
    """Drop images, fonts and video. They are never needed to clear a challenge."""
    if route.request.resource_type in _BLOCKED_RESOURCES:
        await route.abort()
        return
    await route.continue_()


@asynccontextmanager
async def session() -> AsyncGenerator[Session]:
    """Launch a stealth browser, yield a ready page, and always tear it down.

    Serialised by `_MINT_LOCK`; callers may be queued behind an in-flight mint.
    """
    waited = time.perf_counter()
    async with _MINT_LOCK:
        queued_ms = int((time.perf_counter() - waited) * 1000)
        if queued_ms > 50:
            logger.info("mint queued %dms behind an in-flight browser", queued_ms)

        async with InvisiblePlaywright(
            headless=True,
            proxy=proxy_config(),
            humanize=True,
            locale=BROWSER_LOCALE,
            extra_prefs={"devtools.jsonview.enabled": False},
        ) as raw:
            browser = cast("Browser", raw)
            context = await browser.new_context()
            page = await context.new_page()

            if BLOCK_MEDIA:
                await page.route("**/*", _block_media)

            yield Session(page, context)
