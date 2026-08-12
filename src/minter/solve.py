

"""Challenge detection, solving, and cookie extraction.

Cloudflare's non-interactive interstitial clears *itself* once the browser executes
the challenge JS successfully — there is nothing to click. Clicking is only needed
for the interactive variant (a checkbox in an iframe). So the strategy is: wait for
clearance, and reach for the ClickSolver only if waiting stalls.
"""

import asyncio
import contextlib
import re
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright_captcha import CaptchaType
from playwright_captcha.utils.exceptions import CaptchaDetectionError

from minter.browser import Budget, click_solver, session
from minter.config import configure_logging
from minter.models import Cookie, FetchResponse, MintResponse

logger = configure_logging()

# How long to let the interstitial clear on its own before trying to click anything.
_PASSIVE_WAIT_S = 12.0
# Poll interval while watching for the clearance cookie.
_POLL_S = 0.5
# Bounded wait after a click attempt. An unclearable challenge should fail fast
# rather than consume the caller's whole budget.
_POST_CLICK_WAIT_S = 15.0
# Grace period after navigation for an interstitial to render before we judge
# whether one is present. Without it, detection races the challenge.
_APPEAR_S = 1.5


class NoClearanceError(RuntimeError):
    """The page settled but Cloudflare issued no cf_clearance cookie."""


class ChallengeNotClearedError(RuntimeError):
    """The interstitial was still on screen when the budget ran out."""


async def challenge_kind(page: Page) -> str:
    """Best-effort name for the challenge blocking us, for error messages.

    Cloudflare states it in `cType` on the challenge page: "non-interactive" clears
    itself, "interactive" needs a Turnstile widget clicked and is a different beast.
    """
    try:
        html = await page.content()
    except Exception:  # noqa: BLE001
        return "unknown"
    m = re.search(r"cType:\s*'([^']+)'", html)
    kind = m.group(1) if m else "unknown"
    if "cf-turnstile-response" in html or "challenges.cloudflare.com/turnstile" in html:
        kind += " (turnstile widget)"
    return kind


# Cloudflare's non-interactive interstitial. Matched loosely: the title is the most
# reliable signal, the selectors cover the case where the title has already flipped
# but the challenge frame is still mounted.
_CHALLENGE_TITLES = {"just a moment...", "just a moment", "attention required!"}
# Locale-independent markers emitted only while a challenge is running. Measured
# against real pages: present 7x/3x on an interstitial, 0x once cleared. Notably
# NOT "challenge-platform" — Cloudflare leaves that telemetry script on pages that
# have already cleared, so matching it reports a challenge forever.
_CHALLENGE_MARKERS = ("cf_chl_opt", "__cf_chl")


async def detect_challenge(page: Page) -> bool:
    """True when the page is showing a Cloudflare interstitial rather than content.

    Markers are checked before the title because the title is English-only: with
    BROWSER_LOCALE=auto the browser adopts the exit IP's language, so a non-English
    exit renders "Un momento…" and title matching silently fails. `cf_chl_opt` and
    `__cf_chl` are emitted regardless of locale, and measurement confirms they are
    absent from cleared pages (unlike `challenge-platform`, which lingers).

    Failures to read the document mean it is mid-replacement, which is exactly what
    a challenge does — so those count as "challenged", never as "clear".
    """
    try:
        html = (await page.content()).lower()
    except Exception:  # noqa: BLE001 - document being replaced; assume still challenged
        return True

    if any(marker in html for marker in _CHALLENGE_MARKERS):
        return True

    try:
        return (await page.title()).strip().lower() in _CHALLENGE_TITLES
    except Exception:  # noqa: BLE001 - same reasoning as above
        return True


def domain_matches(domain: str, host: str) -> bool:
    """True when a cookie scoped to `domain` would be sent to `host`."""
    d = domain.lstrip(".")
    return host == d or host.endswith("." + d)


async def _has_clearance(context: BrowserContext, host: str) -> bool:
    """True only when clearance exists *for the target host*.

    Matching on name alone is a trap: a mint also picks up a cf_clearance scoped to
    .cloudflare.com, which is useless for the site we came for. Accepting it makes
    the wait below exit early — before the real cookie is issued — and produces an
    apparently successful mint that fails on first use.
    """
    return any(
        c["name"] == "cf_clearance" and domain_matches(c["domain"], host)
        for c in await context.cookies()
    )


async def _await_clearance(page: Page, budget: Budget, limit_s: float) -> bool:
    """Poll until the interstitial is gone — i.e. the real page has rendered.

    Waiting on the cf_clearance cookie instead looks equivalent but is not: the
    cookie is set *before* the interstitial finishes and hands over to the real
    document. Navigating on the cookie therefore leaves the site half-way through
    clearing, and the next request lands back on a challenge that never resolves.
    The page title is the signal that the handover actually completed.
    """
    deadline = min(limit_s, budget.remaining())
    waited = 0.0
    while waited < deadline:
        if not await detect_challenge(page):
            return True
        await asyncio.sleep(_POLL_S)
        waited += _POLL_S
    return False


def _watch_user_agent(page: Page) -> dict[str, str]:
    """Capture the UA from the document request headers.

    Reading it via `page.evaluate("navigator.userAgent")` fails outright on
    CSP-strict sites ("call to eval() blocked by CSP"), so the header is the
    reliable source and evaluate() is only a fallback.
    """
    seen: dict[str, str] = {}

    def on_request(req) -> None:  # noqa: ANN001 - playwright Request
        if "ua" not in seen and req.resource_type == "document":
            ua = req.headers.get("user-agent")
            if ua:
                seen["ua"] = ua

    page.on("request", on_request)
    return seen


async def _resolve_user_agent(page: Page, seen: dict[str, str]) -> str:
    if ua := seen.get("ua"):
        return ua
    try:
        return await page.evaluate("navigator.userAgent")
    except Exception:  # noqa: BLE001 - CSP can block eval; the header path is primary
        logger.warning("could not read user agent")
        return ""


async def _goto(page, url: str, budget: Budget, referer: str | None = None) -> None:
    """Navigate, tolerating the challenge page reloading itself mid-navigation.

    Cloudflare's interstitial re-navigates to the same URL as part of clearing, which
    Playwright surfaces as "interrupted by another navigation". The navigation still
    lands, so this is noise rather than failure.

    `referer` matters for deep links: a bare goto sends none, so the request looks
    like a cold deep-link hit even after the site root has been cleared.
    """
    kwargs = {"wait_until": "domcontentloaded", "timeout": budget.remaining_ms()}
    if referer:
        kwargs["referer"] = referer
    try:
        await page.goto(url, **kwargs)
    except PlaywrightError as exc:
        if "interrupted by another navigation" not in str(exc):
            raise
        logger.info("navigation self-redirected (challenge reload); continuing")
        # The clearance poll below is the real check, so a missed load state is fine.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state(
                "domcontentloaded", timeout=min(20_000, budget.remaining_ms())
            )


async def _settle(page, budget: Budget, max_s: float = 10.0) -> None:
    """Wait for the document to finish loading, bounded by the budget."""
    with contextlib.suppress(Exception):
        await page.wait_for_load_state(
            "load", timeout=min(max_s * 1000, budget.remaining_ms())
        )


async def _goto_and_clear(
    page, url: str, budget: Budget, referer: str | None = None
) -> bool:
    """Navigate to `url` and clear any interstitial. Returns whether one was present."""
    await _goto(page, url, budget, referer)

    # Checking for the interstitial the instant goto() returns races it: the
    # challenge often has not rendered yet, so detection reports "clear" and the
    # caller receives a half-built transition document. Give it a moment to appear.
    await asyncio.sleep(_APPEAR_S)

    if not await detect_challenge(page):
        logger.info("no challenge at %s", url)
        await _settle(page, budget)
        return False

    logger.info("interstitial at %s — waiting for it to clear", url)
    if not await _await_clearance(page, budget, _PASSIVE_WAIT_S):
        # Still stuck: this is likely the interactive variant, so try clicking.
        logger.info("still challenged after %.0fs — trying ClickSolver", _PASSIVE_WAIT_S)
        try:
            async with click_solver(page) as solver:
                await solver.solve_captcha(page, CaptchaType.CLOUDFLARE_INTERSTITIAL)
        except CaptchaDetectionError:
            # No iframe to click. Nothing more to do but keep waiting.
            logger.info("no clickable widget present; continuing to wait")
        except Exception:  # noqa: BLE001 - solver failure is not fatal on its own
            logger.warning("ClickSolver failed; continuing to wait")

        # Bounded, not "whatever is left" — an unclearable challenge should fail in
        # seconds, not burn the caller's entire timeout budget.
        await _await_clearance(page, budget, _POST_CLICK_WAIT_S)

    if await detect_challenge(page):
        logger.warning("gave up on %s — still showing the interstitial", url)
    else:
        # The handover to the real document is a fresh load; wait for it or the
        # caller gets the transition page instead of the content.
        await _settle(page, budget)
    return True


async def _open(page, url: str, budget: Budget) -> bool:
    """Open `url`, warming up on the site root first when the target is a deep link.

    Navigating cold straight to a deep path (e.g. /search/...) reliably gets an
    interstitial that never clears, while the site root clears in a few seconds.
    Landing on the root first and then following the link is both what a real user
    does and what actually works.
    """
    parts = urlparse(url)
    root = f"{parts.scheme}://{parts.netloc}/"

    solved = False
    referer = None
    if parts.path not in ("", "/"):
        logger.info("warming up on %s before %s", root, parts.path)
        solved = await _goto_and_clear(page, root, budget)
        referer = root

    solved_target = await _goto_and_clear(page, url, budget, referer=referer)
    return solved or solved_target


async def mint(url: str, timeout: int) -> MintResponse:
    """Visit `url`, clear any challenge, and return the resulting cookies.

    Raises NoClearanceError when the page settles without a cf_clearance cookie —
    an empty success would be indistinguishable from a working mint to the caller.
    """
    host = urlparse(url).hostname or ""

    async with session() as (page, context):
        # Clock starts once we hold the browser, not while queued for it.
        budget = Budget(timeout)
        seen_ua = _watch_user_agent(page)
        solved = await _open(page, url, budget)

        raw_cookies = await context.cookies()
        user_agent = await _resolve_user_agent(page, seen_ua)
        final_url = page.url

    cookies = [
        Cookie(**{k: v for k, v in c.items() if k in Cookie.model_fields})
        for c in raw_cookies
    ]

    if not any(c.name == "cf_clearance" and domain_matches(c.domain, host) for c in cookies):
        raise NoClearanceError(
            f"no cf_clearance scoped to {host} "
            f"(got: {sorted((c.name, c.domain) for c in cookies)})"
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


async def fetch(url: str, timeout: int) -> FetchResponse:
    """Fetch a page through the browser, clearing any challenge first.

    This exists because cf_clearance cannot be reused by an ordinary HTTP client.
    Cloudflare binds the cookie to the issuing TLS fingerprint, and the patched
    Firefox 151 ClientHello has no equivalent in uTLS (whose newest Firefox profile
    is 120) — measured JA3, JA4 and HTTP/2 SETTINGS all differ. Fetching in-browser
    removes fingerprint matching from the problem entirely.
    """
    async with session() as (page, _context):
        # Start the clock only once we hold the browser. Creating the budget before
        # entering session() charged queued callers for time spent waiting on the
        # lock, so a request behind a slow one could 408 without ever running.
        budget = Budget(timeout)
        seen_ua = _watch_user_agent(page)
        challenged = await _open(page, url, budget)

        # `solved` must mean cleared, not merely "a challenge appeared". Returning
        # the interstitial with solved=true made every failure look like a success.
        still_blocked = await detect_challenge(page)
        if challenged and still_blocked:
            kind = await challenge_kind(page)
            raise ChallengeNotClearedError(
                f"challenge not cleared for {url} (cType={kind}). "
                f"Interactive Turnstile is not solvable by this service."
            )

        html = await page.content()
        user_agent = await _resolve_user_agent(page, seen_ua)
        final_url = page.url
        solved = challenged

    logger.info(
        "fetched %s in %dms (%d bytes, solved=%s)", url, budget.elapsed_ms(), len(html), solved
    )

    return FetchResponse(
        solved=solved,
        html=html,
        final_url=final_url,
        user_agent=user_agent,
        elapsed_ms=budget.elapsed_ms(),
    )


async def probe_user_agent(url: str = "https://example.com/", timeout: int = 45) -> str:
    """Launch a browser and report its User-Agent. Used by /health as a real end-to-end check."""
    async with session() as (page, _context):
        budget = Budget(timeout)
        seen_ua = _watch_user_agent(page)
        await page.goto(url, wait_until="domcontentloaded", timeout=budget.remaining_ms())
        return await _resolve_user_agent(page, seen_ua)
