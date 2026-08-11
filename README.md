# minter

Mints Cloudflare clearance cookies. Replaces both `flaresolverr` and `byparr` with one small service.

## What it does

FlareSolverr and Byparr answer *"fetch this page for me"*, which forces a browser into the path of
every request. `minter` answers *"get me a cookie that works"* — the browser produces a credential
and exits, and the caller makes ordinary HTTP requests afterwards.

`cf_clearance` is valid for roughly 30 minutes to two hours, so the browser runs about once an hour
instead of once per request.

## API

```
POST /mint   {"url": "https://1337x.to/"}
          →  {"solved": true, "user_agent": "Mozilla/5.0 ... Firefox/…",
              "cookies": [...], "elapsed_ms": 4210}

GET  /health →  {"ok": true, "user_agent": "…"}
```

Returns **502** if a mint completes without producing a `cf_clearance` cookie, and **408** if the
timeout budget is exhausted.

## Using the cookie — read this before writing a client

### Match the fingerprint

Cloudflare binds `cf_clearance` to three things:

1. the **exit IP** it was issued to,
2. the **User-Agent** string,
3. the **TLS fingerprint** (JA3).

`invisible-playwright` patches **Firefox**, so the cookie is issued to a *Firefox* fingerprint. A Go
client must therefore use `utls.HelloFirefox_Auto` — **not** `HelloChrome_Auto`, the obvious choice —
and send back the exact `user_agent` this service returned. A mismatch produces a 403 that is
indistinguishable from having no cookie at all.

The UA is **not stable between mints**. `invisible-playwright` derives a fresh persona per session,
so consecutive calls return different User-Agents (frequently a Windows one, whatever host you run
on). Always pair the cookie with the UA from the *same* response; never hardcode it.

If a matched JA3 still returns 403, force HTTP/1.1 to rule out the HTTP/2 `SETTINGS` fingerprint.

### Filter cookies by domain

A single mint returns cookies for **more than one domain**. Observed against 1337x:

```
cf_clearance   domain=.1337x.to          ← the one you want
cf_chl_rc_ni   domain=1337x.to
cf_clearance   domain=.cloudflare.com    ← NOT for the target host
```

Selecting `cf_clearance` by name alone can hand Cloudflare's own clearance to the target site, which
fails with a 403 that looks exactly like a broken solver. Match on domain as well — or feed the whole
list into a real cookie jar (`net/http/cookiejar`) and let it scope them.

## Running

```bash
uv sync
uv run python main.py            # http://localhost:8191
```

Docker:

```bash
docker compose up --build
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8191` | Listen port |
| `LOG_LEVEL` | `INFO` | Logging level |
| `MAX_ATTEMPTS` | `5` | Solver attempts before giving up |
| `DEFAULT_TIMEOUT` | `60` | Seconds per mint |
| `BROWSER_LOCALE` | `auto` | BCP-47 tag; `auto` derives from exit IP |
| `BLOCK_MEDIA` | `true` | Skip images/fonts/video during the mint |
| `PROXY_SERVER` | none | Optional upstream proxy |

## Design notes

- **Stateless.** No cookie cache — that belongs to the caller, so this service can restart without
  losing a valid credential.
- **One browser at a time.** An `asyncio.Lock` serialises mints; concurrent requests share the
  running mint rather than launching parallel Firefox instances.
- **Bounded retries.** `MAX_ATTEMPTS` defaults to 5. Byparr defaults to `sys.maxsize`.

## Credits

The approach follows [ThePhaseless/Byparr](https://github.com/ThePhaseless/Byparr). The actual
anti-detection is provided by [`invisible-playwright`](https://pypi.org/project/invisible-playwright/)
and [`playwright-captcha`](https://pypi.org/project/playwright-captcha/).
