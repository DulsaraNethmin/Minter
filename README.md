# minter

A small service that gets you past Cloudflare's browser challenge — either as a
**clearance cookie** or as the **rendered HTML** of the page you wanted.

It is a deliberately narrow alternative to [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)
and [Byparr](https://github.com/ThePhaseless/Byparr): two endpoints, no proxy
protocol, no compatibility envelope.

```
POST /fetch  {"url": "https://example.com/page"}  →  {"html": "...", "solved": true}
POST /mint   {"url": "https://example.com/"}      →  {"cookies": [...], "user_agent": "..."}
```

> **On the name.** This started out as a cookie *minter*: obtain `cf_clearance`
> once, then replay it cheaply from an ordinary HTTP client. Measurement killed
> that plan — Cloudflare binds the cookie to a TLS fingerprint no off-the-shelf
> client reproduces (see [below](#why-the-browser-runs-per-request)) — so `/fetch`
> became the endpoint that works and `/mint` stayed for callers who can match the
> fingerprint. The name is a fossil of the original idea, kept because it is short.

---

## How it works

`minter` drives a stealth-patched Firefox. The anti-detection is not this project's
code — it comes from [`invisible-playwright`](https://pypi.org/project/invisible-playwright/),
which ships a Firefox built with its fingerprints compiled **into the engine**
rather than injected into the page. This service is the orchestration around it:
lifecycle, challenge detection, timeouts, and a narrow HTTP surface.

```mermaid
flowchart TB
    caller["your application"]
    subgraph minter["minter"]
        api["FastAPI<br/>/fetch · /mint · /health"]
        lock["asyncio.Lock<br/>one browser at a time"]
        solve["challenge detection<br/>+ clearance wait"]
    end
    subgraph browser["browser (per request)"]
        ff["patched Firefox 151<br/>~200 seed-derived fingerprint fields"]
        click["ClickSolver<br/>(lazy — fallback only)"]
    end
    site["Cloudflare-protected site"]

    caller -->|"POST /fetch"| api
    api --> lock
    lock --> solve
    solve --> ff
    solve -.->|"only if waiting stalls"| click
    ff <-->|"navigate, run challenge JS"| site
    solve -->|"html + cookies + user_agent"| caller
```

### The request flow

The key insight is that Cloudflare's non-interactive interstitial **clears itself**
once a sufficiently real browser executes its JavaScript. There is nothing to click.
Clicking is only needed for the interactive variant, so the solver is a fallback
rather than the main path.

```mermaid
sequenceDiagram
    participant C as caller
    participant M as minter
    participant B as Firefox
    participant CF as site + Cloudflare

    C->>M: POST /fetch {url}
    M->>M: acquire lock, start timeout budget
    M->>B: launch browser

    Note over M,CF: deep links need a warm-up first
    B->>CF: GET site root
    CF-->>B: interstitial
    B->>B: run challenge JS
    CF-->>B: cf_clearance + real page

    B->>CF: GET target (with referer)
    CF-->>B: page

    M->>M: poll until cf_chl_opt marker is gone
    M->>M: wait for load, capture html
    M->>B: tear down
    M-->>C: {html, cookies, user_agent, solved}
```

### Why the browser runs per request

`cf_clearance` cannot be handed to an ordinary HTTP client. Cloudflare binds it to
the **exit IP, User-Agent and TLS fingerprint** of whatever obtained it, and the
patched Firefox 151 has no equivalent in uTLS. Measured against `tls.peet.ws`:

| | patched Firefox 151 | uTLS `HelloFirefox_Auto` (= FF 120) |
|---|---|---|
| JA3 | `6447ab086255d194909d4013b1a89e87` | `b5001237acdf006056b409cc433726b0` |
| JA4 | `t13d1617h2_86a278354501_…` | `t13d1715h2_5b57614c22b0_…` |
| HTTP/2 | `1:65536;2:0;4:131072;5:16384` | `2:0;4:4194304;5:16384;6:10485760` |

The browser also offers TLS extensions `18` and `27` and curve `4588`
(X25519MLKEM768) that Firefox 120 predates. All three fingerprints differ, and a
reused cookie is rejected with a 403 indistinguishable from having no cookie.

`/mint` is still provided for callers that *can* reproduce the fingerprint, but
**`/fetch` is the endpoint that works**.

---

## Does it work on other sites?

**Yes — there is no site-specific code.** Both endpoints take an arbitrary URL, and
the deep-link warm-up derives the site root from that URL. Verified unchanged
against `example.com` (no protection), `nowsecure.nl` (a standard Cloudflare
bot-detection test) and `1337x.to` (active interstitial).

What varies is not the site but the **challenge type**:

| Challenge | Supported | Notes |
|---|:--:|---|
| Non-interactive interstitial (*"Just a moment…"*) | ✅ | The common case. Clears passively in ~3–8 s |
| Turnstile (interactive checkbox) | ⚠️ | `ClickSolver` fallback exists but is untested here |
| Managed challenge with a real CAPTCHA | ❌ | Needs a paid API solver; not wired up |
| Hard block (Error 1020, IP ban) | ❌ | Not a challenge — nothing to solve |
| DataDome / PerimeterX / Akamai | ❌ | Different vendors entirely |

Detection is deliberately locale-independent. It keys on the `cf_chl_opt` and
`__cf_chl` markers rather than the page title, because with `BROWSER_LOCALE=auto`
the browser adopts the exit IP's language and an English title match would silently
fail behind a VPN. Measured on real pages, those markers appear 7× and 3× on an
interstitial and 0× once cleared.

So: pointing it at a new Cloudflare-protected site needs **no code changes**. A
different anti-bot vendor needs a different tool.

---

## API

### `POST /fetch`

Fetch a page, clearing any challenge first. This is what most callers want.

```jsonc
// request
{ "url": "https://example.com/search/thing/1/", "timeout": 120 }

// response
{
  "solved": true,          // a challenge was present and cleared
  "html": "<!DOCTYPE html>…",
  "final_url": "https://example.com/search/thing/1/",
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; …) Firefox/151.0",
  "elapsed_ms": 7878
}
```

### `POST /mint`

Return the clearance cookies instead of the page. Only useful if your client can
present the same TLS fingerprint — see the table above.

```jsonc
{
  "solved": true,
  "cookies": [ { "name": "cf_clearance", "domain": ".example.com", … } ],
  "user_agent": "Mozilla/5.0 … Firefox/151.0",
  "elapsed_ms": 6139
}
```

Returns **502** if no `cf_clearance` scoped to the target host was issued, and
**408** if the timeout budget runs out. A cookie scoped to `.cloudflare.com` does
not count — that one is always present and is useless for the target site.

### `GET /health`

Drives a real browser end to end. Slow by design; not a liveness ping.

---

## Running it

```bash
uv sync
uv run python main.py          # http://localhost:8191
```

Docker:

```bash
docker compose up --build
```

The image is ~2.2 GB, almost entirely Firefox (564 MB) plus its system libraries
(596 MB) and a GeoIP database (119 MB). It idles at ~125 MB RSS and peaks around
450 MB while a browser is running.

Interactive API docs are at `/docs`.

### A quick search example

`search.sh` demonstrates the intended use — fetch, then parse on your side:

```bash
./search.sh "Interstellar"
```

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8191` | Listen port |
| `LOG_LEVEL` | `INFO` | Logging level |
| `MAX_ATTEMPTS` | `5` | ClickSolver attempts before giving up |
| `DEFAULT_TIMEOUT` | `60` | Seconds per request |
| `BROWSER_LOCALE` | `auto` | BCP-47 tag; `auto` derives from the exit IP |
| `BLOCK_MEDIA` | `true` | Skip images/fonts/video while solving |
| `PROXY_SERVER` | — | Optional upstream proxy |

Leave `BROWSER_LOCALE` on `auto`: a locale that disagrees with the exit IP is
itself a detection signal.

---

## Design notes

Four decisions that differ from FlareSolverr and Byparr, each for a measured reason.

**One browser at a time.** An `asyncio.Lock` serialises requests; concurrent callers
queue rather than launching parallel Firefox instances. On small hardware three at
once means swapping. The timeout budget starts *after* the lock is acquired, so a
queued caller is not charged for waiting.

**The solver is lazy.** Entering `ClickSolver` installs handlers on the page, and
doing so eagerly stops the non-interactive interstitial clearing on its own — every
request degrades into five failed click attempts. It is now constructed only when
passive waiting has already failed.

**Clearance is judged by the page, not the cookie.** `cf_clearance` is set *before*
the interstitial hands over to the real document. Acting on the cookie leaves the
site mid-clearance and the next request lands on a challenge that never resolves.

**Deep links warm up first.** Navigating cold to `/some/deep/path` reliably yields
an interstitial that never clears, while the site root clears in seconds. Requests
therefore land on the root first and follow through with a referer — which is also
what a real user does.

**Stateless.** No cookie cache. Callers own that, so this service can be restarted
or rebuilt without losing anything.

---

## Limitations

- **It is an arms race.** When Cloudflare changes, the fix comes from
  `invisible-playwright` upstream and you take the version bump.
- **Only Cloudflare**, and only the challenge types in the table above.
- **The User-Agent is not stable.** A fresh persona is derived per session, so
  consecutive calls return different User-Agents (often a Windows one regardless of
  host). Always pair a cookie with the UA from the same response; never hardcode it.
- **The Ubuntu base is pinned to 24.04.** `ubuntu:latest` floats to 26.04, for which
  Playwright cannot install Firefox dependencies.

---

## Development

```bash
uv run pytest                 # all tests, including one live 1337x fetch
uv run pytest -m 'not live'   # unit tests only, no browser
uv run ruff check .
```

---

## Credits

The approach follows [ThePhaseless/Byparr](https://github.com/ThePhaseless/Byparr).
The anti-detection comes from
[`invisible-playwright`](https://pypi.org/project/invisible-playwright/) and
[`playwright-captcha`](https://pypi.org/project/playwright-captcha/) — this project
is the orchestration around them, not the evasion itself.

## License

MIT
