"""Request and response shapes.

Deliberately not FlareSolverr-compatible: there is no `cmd`, no version echo and no
timestamp envelope, because nothing consumes them. The only consumer is our own Go
client, which needs exactly two things — the cookies and the User-Agent they were
issued to.
"""

from typing import Any

from pydantic import BaseModel, Field

from minter.config import DEFAULT_TIMEOUT


class MintRequest(BaseModel):
    url: str = Field(
        pattern=r"^https?://",
        description="Page to visit. Any URL on the target host will do; the cookie is host-scoped.",
        examples=["https://1337x.to/"],
    )
    timeout: int = Field(
        default=DEFAULT_TIMEOUT,
        ge=5,
        le=300,
        description="Seconds allowed for the whole mint.",
    )


class Cookie(BaseModel):
    name: str
    value: str
    domain: str
    path: str
    expires: float | None = None
    httpOnly: bool = False  # noqa: N815 - Playwright's casing, kept for pass-through
    secure: bool = False
    sameSite: str | None = None  # noqa: N815 - Playwright's casing


class MintResponse(BaseModel):
    solved: bool = Field(description="True when a challenge was detected and cleared.")
    cookies: list[Cookie]
    user_agent: str = Field(
        description=(
            "The exact UA the browser presented. Clients MUST send this back verbatim — "
            "cf_clearance is bound to it."
        )
    )
    elapsed_ms: int
    final_url: str


class HealthResponse(BaseModel):
    ok: bool
    user_agent: str
    detail: dict[str, Any] = Field(default_factory=dict)
