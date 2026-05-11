# Privacy policy

**estonian-mcp** is a public, free Estonian-NLP MCP server hosted by
Silly Geese Solutions at `https://estonian-mcp.fly.dev`. This
document describes what data passes through the service and what we
do (and do not do) with it.

## What data we receive

When you (or your AI agent) call the server, the request contains:

- The Estonian text or word you sent as a tool argument.
- Standard HTTP metadata: client IP (used only for per-IP rate
  limiting), `User-Agent`, request timing.

We receive no user identity, account, email, name, or any other
profile information. The service is unauthenticated; there is no
account to create.

## What we do with it

- **Process the text** through EstNLTK (tokenization, morphology,
  spell-check, NER, WordNet, fastText related-words, register
  classification) and return the result to your client.
- **Rate-limit per IP** in memory. The bucket key (your IP) lives in
  the running process and is **lost when the machine restarts**. We
  do not persist rate-limit state across restarts.
- **Run platform health checks** on `/health` from Fly.io's edge.

## What we do NOT do

- **No request logging.** Uvicorn's access log is explicitly disabled
  (`access_log=False` in `server.py`). The bodies of your requests
  (the Estonian text you analyse) are never written to disk, never
  shipped to a log aggregator, never observable by us.
- **No analytics or telemetry.** The server makes no outbound HTTP
  calls and reports no usage to any third party.
- **No third-party processors.** All analysis runs locally inside the
  server container. We do not call OpenAI, Anthropic, Google, or any
  other inference provider. EstNLTK and Vabamorf models are bundled
  into the Docker image; WordNet and fastText resources are
  pre-downloaded at image-build time and served from the container
  filesystem.
- **No cookies, no tracking, no fingerprinting.**
- **No training.** Your inputs are not used to train any model. The
  fastText, WordNet, and morphological models bundled with the
  server are static; they don't update from runtime traffic.

## Retention

- Tool inputs and outputs: **0 seconds**. They exist only in process
  memory for the duration of the request and are discarded when the
  response is sent.
- Per-IP rate-limit counters: in-memory only, lost on any machine
  restart (typically every few hours under auto-stop-when-idle).
- Operational logs (boot, shutdown, errors): retained by Fly.io
  according to their default retention; no user data appears in them
  because we don't log request bodies.

## Where data is processed

In a single Fly.io machine running in **Amsterdam (ams region)**,
EU.

## Cookies

None.

## Children

The service is a generic NLP API; it has no awareness of who is
calling it and no UI directed at any audience. Operators of LLM
clients integrating this server are responsible for compliance with
relevant laws (COPPA, GDPR-K, etc.) in their own user-facing apps.

## Your rights

Because we don't store any data tied to you, there is nothing to
delete, export, or rectify. If you stop calling the server, all
trace of your usage is gone.

## Source code

The server is open source under Apache-2.0 at
`https://github.com/silly-geese/estonian-mcp`. Anyone can verify
the claims above by reading `server.py` (~400 lines, one file). The
auth-and-logging surface lives in `_build_http_app`,
`_extract_token`, and `_RateLimiter` in the same file.

## Security issues

Report vulnerabilities via GitHub Security Advisories at
`https://github.com/silly-geese/estonian-mcp/security/advisories/new`
— see [SECURITY.md](SECURITY.md) for the full disclosure process.

## Changes to this policy

This policy may be updated; substantive changes will appear in git
history. The latest version is always at
`https://github.com/silly-geese/estonian-mcp/blob/master/PRIVACY.md`.

Last updated: 2026-05-10.
