"""Challenge detection, solving, and cookie extraction.

Cloudflare's non-interactive interstitial clears *itself* once the browser executes
the challenge JS successfully — there is nothing to click. Clicking is only needed
for the interactive variant (a checkbox in an iframe). So the strategy is: wait for
clearance, and reach for the ClickSolver only if waiting stalls.
"""

import asyncio

from playwright.async_api import BrowserContext, Page
from playwright_captcha import CaptchaType
from playwright_captcha.utils.exceptions import CaptchaDetectionError

from minter.browser import Budget, session
from minter.config import configure_logging
from minter.models import Cookie, MintResponse

logger = configure_logging()

# How long to let the interstitial clear on its own before trying to click anything.
_PASSIVE_WAIT_S = 12.0
# Poll interval while watching for the clearance cookie.
_POLL_S = 0.5


class NoClearanceError(RuntimeError):
    """The page settled but Cloudflare issued no cf_clearance cookie."""


# Cloudflare's non-interactive interstitial. Matched loosely: the title is the most
# reliable signal, the selectors cover the case where the title has already flipped
# but the challenge frame is still mounted.
_CHALLENGE_TITLES = {"just a moment...", "just a moment", "attention required!"}
# Only containers that belong to the interstitial itself. Notably NOT
# `script[src*='challenge-platform']` — Cloudflare leaves that telemetry script on
# pages that have already cleared, so matching it reports a challenge forever.
_CHALLENGE_SELECTORS = (
    "#challenge-running",
    "#challenge-form",
    "#cf-challenge-running",
)


async def detect_challenge(page: Page) -> bool:
    """True when the page is showing a Cloudflare interstitial rather than content."""
    try:
        title = (await page.title()).strip().lower()
    except Exception:  # noqa: BLE001 - a detached page is simply not a challenge
        title = ""

    if title in _CHALLENGE_TITLES:
        return True

    for selector in _CHALLENGE_SELECTORS:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001 - navigation mid-check
            continue

    return False


async def _has_clearance(context: BrowserContext) -> bool:
    return any(c["name"] == "cf_clearance" for c in await context.cookies())


async def _await_clearance(
    page: Page, context: BrowserContext, budget: Budget, limit_s: float
) -> bool:
    """Poll until clearance appears, the interstitial disappears, or time runs out."""
    deadline = min(limit_s, budget.remaining())
    waited = 0.0
    while waited < deadline:
        if await _has_clearance(context):
            return True
        if not await detect_challenge(page):
            return True
        await asyncio.sleep(_POLL_S)
        waited += _POLL_S
    return False


async def mint(url: str, timeout: int) -> MintResponse:
    """Visit `url`, clear any challenge, and return the resulting cookies.

    Raises NoClearanceError when the page settles without a cf_clearance cookie —
    an empty success would be indistinguishable from a working mint to the caller.
    """
    budget = Budget(timeout)

    async with session() as (page, solver, context):
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=budget.remaining_ms(),
        )

        solved = False
        if await detect_challenge(page):
            logger.info("interstitial at %s — waiting for it to clear", url)
            solved = True

            if not await _await_clearance(page, context, budget, _PASSIVE_WAIT_S):
                # Still stuck: this is likely the interactive variant, so try clicking.
                logger.info("still challenged after %.0fs — trying ClickSolver", _PASSIVE_WAIT_S)
                try:
                    await solver.solve_captcha(page, CaptchaType.CLOUDFLARE_INTERSTITIAL)
                except CaptchaDetectionError:
                    # No iframe to click. Nothing more to do but keep waiting.
                    logger.info("no clickable widget present; continuing to wait")
                except Exception:  # noqa: BLE001 - solver failure is not fatal on its own
                    logger.exception("ClickSolver raised; continuing to wait")

                await _await_clearance(page, context, budget, budget.remaining())

            if not await _has_clearance(context):
                logger.warning("gave up on %s without clearance", url)
        else:
            logger.info("no challenge at %s", url)

        raw_cookies = await context.cookies()
        user_agent = await page.evaluate("navigator.userAgent")
        final_url = page.url

    cookies = [
        Cookie(**{k: v for k, v in c.items() if k in Cookie.model_fields})
        for c in raw_cookies
    ]

    if not any(c.name == "cf_clearance" for c in cookies):
        raise NoClearanceError(
            f"no cf_clearance issued for {url} (got: {sorted(c.name for c in cookies)})"
        )

    logger.info(
        "minted cf_clearance for %s in %dms (solved=%s)", url, budget.elapsed_ms(), solved
    )

    return MintResponse(
        solved=solved,
        cookies=cookies,
        user_agent=user_agent,
        elapsed_ms=budget.elapsed_ms(),
        final_url=final_url,
    )


async def probe_user_agent(url: str = "https://example.com/", timeout: int = 45) -> str:
    """Launch a browser and report its User-Agent. Used by /health as a real end-to-end check."""
    budget = Budget(timeout)
    async with session() as (page, _solver, _context):
        await page.goto(url, wait_until="domcontentloaded", timeout=budget.remaining_ms())
        return await page.evaluate("navigator.userAgent")
