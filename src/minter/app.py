"""FastAPI surface: two endpoints, no compatibility envelope."""

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from minter.config import configure_logging
from minter.models import HealthResponse, MintRequest, MintResponse
from minter.solve import NoClearanceError, mint, probe_user_agent

logger = configure_logging()

app = FastAPI(
    title="minter",
    description="Mints Cloudflare clearance cookies. Replaces flaresolverr and byparr.",
    version="0.1.0",
)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.post("/mint", response_model=MintResponse)
async def mint_endpoint(req: MintRequest) -> MintResponse:
    """Visit the URL, clear any Cloudflare challenge, return the cookies.

    The `user_agent` in the response must be sent back verbatim by the caller —
    cf_clearance is bound to it, along with the exit IP and the TLS fingerprint.
    """
    try:
        return await mint(req.url, req.timeout)
    except NoClearanceError as exc:
        # Solved-but-no-cookie is a real, distinct failure; do not report success.
        logger.warning("mint produced no clearance: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (PlaywrightTimeout, TimeoutError) as exc:
        logger.warning("mint timed out for %s", req.url)
        raise HTTPException(
            status_code=408, detail=f"timed out after {req.timeout}s"
        ) from exc
    except PlaywrightError as exc:
        logger.exception("browser error minting %s", req.url)
        raise HTTPException(status_code=502, detail=f"browser error: {exc}") from exc


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Drive a real browser end to end. Slow by design — this is not a liveness ping."""
    try:
        ua = await probe_user_agent()
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        logger.exception("health check failed")
        raise HTTPException(status_code=503, detail=f"browser unavailable: {exc}") from exc
    return HealthResponse(ok=True, user_agent=ua)
