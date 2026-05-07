# Security policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Use GitHub's private vulnerability reporting:
**Security → Advisories → Report a vulnerability** on this repository.
This sends a private report visible only to maintainers.

We aim to acknowledge within 7 days and ship a fix or mitigation
within 30 days for confirmed issues, sooner for high-severity.

## Threat model

`estonian-mcp` is a small ASGI/MCP server that ships in two modes:

- **stdio** — local subprocess launched by the MCP client.
- **streamable-http** — bearer-protected HTTP service for remote
  deployments (Smithery, Fly.io, your own container host).

Each mode has a different attack surface; we describe both.

### stdio mode

Pure local. The expected deployment is a single-user machine where the
MCP client (Claude Desktop, Claude Code, Cursor, Cowork local) launches
the server as a child process and communicates over stdin/stdout.

**What this server does NOT do:**
- **No network egress.** No HTTP requests, no socket connections,
  no DNS lookups in stdio mode. (HTTP mode obviously listens, but
  still does not initiate outbound calls.)
- **No shell execution.** No `os.system`, `subprocess`, `eval`,
  `exec`, or `pickle.loads` of untrusted input.
- **No filesystem writes.** The server only reads code + models that
  ship inside its own Python wheels.
- **No telemetry, no analytics, no phone-home.**

**Inputs treated as untrusted:** tool arguments arriving from the LLM
client. The LLM may have ingested hostile content (prompt injection
from an email, web page, etc.) and forwarded a crafted call. Defences:

- **Resource exhaustion**: every tool caps text input at 100,000 chars
  (200 chars for `syllabify`). Oversized inputs raise `ValueError`
  surfaced as a structured tool error rather than hanging the server.
- **Malformed input**: type checks reject non-string args. EstNLTK
  itself handles malformed Estonian gracefully.

### streamable-http mode

Adds a network attack surface. Two auth postures:

**Bearer mode (default).** Defences:

- **Refuses to start without an auth token.** `ESTNLTK_MCP_AUTH_TOKEN`
  must be set and ≥16 characters; otherwise the process exits with
  status 2.
- **Bearer-token auth on every request.** Token is read from the
  `Authorization: Bearer <token>` header **or** from a Smithery-style
  `?config=<base64-json>` query param (with `apiKey`, `bearerToken`,
  or `token` fields).
- **Constant-time comparison** (`secrets.compare_digest`) to prevent
  timing-based token disclosure.
- **Per-token rate limit.** Default 60 requests/minute, configurable
  via `ESTNLTK_MCP_RATE_LIMIT_PER_MINUTE`.

**Public mode (`ESTNLTK_MCP_PUBLIC_MODE=1`).** Used by the
silly-geese-hosted public Smithery listing. Defences:

- **No bearer auth required.** Anyone on the network can call `/mcp`.
  Intentional, so Smithery installs are one-click.
- **Per-IP rate limit.** Default 120 requests/minute keyed on
  `scope["client"][0]` (populated from `X-Forwarded-For` by uvicorn's
  `proxy_headers=True` so it reflects the originator IP, not Fly's
  edge address).
- **All other hardening preserved.** No shell exec, no fs writes, no
  token logging (no tokens to log), no telemetry, size-bounded inputs.

In either mode, in-process rate-limit state is restart-reset; with
multiple replicas behind a load balancer the effective quota scales
linearly with replica count, which we consider acceptable
defence-in-depth.
- **No request logging, no token logging.** Uvicorn access logs are
  disabled; only operational events (boot, shutdown) are logged.
- **HTTPS termination at the edge.** The server itself listens on
  HTTP — terminate TLS at Fly's load balancer / Smithery's gateway /
  your reverse proxy. `proxy_headers=True` and `forwarded_allow_ips="*"`
  are set so the server trusts the platform's `X-Forwarded-*` headers.
- **Public health endpoint.** `/health` returns `{"ok": true}` with
  no auth and is bypassed by the rate limiter; it is the only
  unauthenticated path. Used for Fly health probes and uptime monitoring.
- **Stateless HTTP.** `mcp.settings.stateless_http = True` so each
  request is independent — no per-client session state to grow
  unbounded.

### Threats we do NOT defend against

- **Compromised host machine.** If your machine is compromised the
  attacker already has stdio access; this server has no privileged
  capabilities to protect.
- **Compromised dependencies.** We pin and lock dependencies via
  `uv.lock` (with hashes) but cannot defend against a malicious
  release of EstNLTK or the Python interpreter itself. Dependabot
  is enabled to surface known CVEs.
- **Token leakage by users.** If you commit your token to a public
  repo or share it, an attacker can hammer your service. Rate limit
  caps the damage, rotate tokens promptly when leaked
  (`fly secrets set ESTNLTK_MCP_AUTH_TOKEN=<new>`).
- **DDoS.** A determined attacker can saturate your container's CPU
  with valid requests up to the rate-limit cap. Use your platform's
  DDoS protections (Fly's edge, Cloudflare in front of Smithery, etc.)
  if this matters to you.
- **Side channels** beyond timing on the token comparison.

## Operational guidance

- **Generate strong tokens:**
  ```sh
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- **Rotate tokens** when team members leave or after any suspected leak.
  Set the new value, redeploy, and update each client.
- **One token per deployment.** If two teams share a deployment,
  they share a token; you cannot revoke one without affecting the
  other. Stand up two deployments with two tokens instead.
- **Watch your platform's logs** for sustained 401s — that's the
  signature of a leaked token being probed.

## Supply chain

- Dependencies pinned and hashed in `uv.lock`, committed to the repo.
- `pyproject.toml` declares minimum versions and an upper Python bound.
- Dependabot alerts enabled for `pip` and `github-actions`.
- CI runs the smoke test on every push + PR before any release.
- Docker image is built from official `python:3.13-slim`; runs as a
  non-root `app` user with no installed shell utilities beyond what
  the base image provides.

## Auditing

The full server is one file (`server.py`, ~370 lines). Read it
end-to-end before deploying. Each `@mcp.tool()` decorator marks a tool
the LLM can call; the function body is what runs. The HTTP wrapper
(`_build_http_app`, `_extract_token`, `_RateLimiter`) is below the
tools and is the entire auth surface.
