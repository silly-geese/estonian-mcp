# Security policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Use GitHub's private vulnerability reporting:
**Security → Advisories → Report a vulnerability** on this repository.
This sends a private report visible only to maintainers.

We aim to acknowledge within 7 days and ship a fix or mitigation
within 30 days for confirmed issues, sooner for high-severity.

## Threat model

`estnltk-mcp` is a **local stdio** MCP server. The expected deployment
is a single-user machine where the MCP client (Claude Desktop, Claude
Code, Cursor, etc.) launches the server as a child process and
communicates over stdin/stdout.

### What this server does NOT do

- **No network egress.** No HTTP requests, no socket connections,
  no DNS lookups. Verifiable by `grep -RE 'requests|urllib|httpx|socket' server.py`.
- **No shell execution.** No `os.system`, `subprocess`, `eval`, `exec`,
  or `pickle.loads` of untrusted input.
- **No filesystem writes.** The server only reads code+models that
  ship inside its own Python wheels.
- **No telemetry, no analytics, no phone-home.**

### Inputs we treat as untrusted

Tool arguments arriving from the LLM client. The LLM may have ingested
hostile content (prompt injection from an email, web page, etc.) and
forwarded a crafted call. Concretely we defend:

- **Resource exhaustion**: every tool caps text input at 100,000 chars
  (200 chars for `syllabify`). Oversized inputs raise `ValueError` and
  are returned to the client as a structured tool error rather than
  hanging the server.
- **Malformed input**: Type checks reject non-string args. EstNLTK
  itself handles malformed Estonian gracefully (it's designed for
  noisy real-world text).

### Threats we explicitly do NOT defend against

- **Compromised host machine.** If your machine is compromised the
  attacker already has stdio access; this server has no privileged
  capabilities to protect.
- **Compromised dependencies.** We pin and lock dependencies via
  `uv.lock` (with hashes) but cannot defend against a malicious
  release of EstNLTK or the Python interpreter itself. Dependabot
  is enabled to surface known CVEs.
- **Side channels** (timing, memory pressure observable from the host).

## Supply chain

- Dependencies pinned and hashed in `uv.lock`, committed to the repo.
- `pyproject.toml` declares minimum versions and an upper Python bound.
- Dependabot alerts are enabled for `pip` and `github-actions`.
- CI runs the smoke test on every push + PR before any release.

## Auditing

The full server is one file (~200 lines). We encourage you to read
`server.py` end-to-end before deploying. There is no hidden code path:
each `@mcp.tool()` decorator marks a tool the LLM can call, and the
function body is what runs.
