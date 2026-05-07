# estonian-mcp

> Claude is quite bad at Estonian, so this MCP/wrapper is here to fix that. Give it a shot.

[![CI](https://github.com/silly-geese/estonian-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/silly-geese/estonian-mcp/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-stdio%20%2B%20HTTP-7c3aed.svg)](https://modelcontextprotocol.io)

A small **Model Context Protocol** server that exposes
[EstNLTK](https://github.com/estnltk/estnltk) — the Estonian NLP toolkit —
as tools any LLM client can call in real time. Hand it Estonian text, get
back correct lemmas, morphology, POS tags, spell-check + suggestions,
syllables, and named entities.

If your AI agent has to draft, edit, or proofread Estonian, this wires
in ground truth so it stops guessing. Two transports:

- **stdio** — local subprocess, zero config, zero network.
- **streamable-http** — bearer-token-protected HTTPS endpoint for
  remote clients (claude.ai web, Claude Cowork remote, Smithery
  hosting, self-hosted Fly.io).

## What it does

| Tool | What it does |
| --- | --- |
| `tokenize(text)` | Sentences + words |
| `analyze_morphology(text, all_analyses=False)` | Lemma, POS, form, root, ending, clitic, compound parts per word |
| `lemmatize(text)` | Just the lemma per word (concise) |
| `pos_tag(text)` | Just the POS tag per word |
| `spell_check(text, suggestions=True)` | Spelling check + correction suggestions |
| `syllabify(word)` | Syllables with quantity + accent for one word |
| `named_entities(text)` | PER / LOC / ORG (CRF model, bundled, no download) |

POS tags: `S`=noun, `V`=verb, `A`=adj, `P`=pron, `D`=adv, `K`=adp,
`J`=conj, `N`=numeral, `I`=interj, `Y`=abbrev, `X`=foreign, `Z`=punct.

## Compatibility

| Client | Transport | Status |
| --- | --- | --- |
| **Claude Desktop** | stdio | ✅ Plug-and-play |
| **Claude Code** | stdio | ✅ Plug-and-play |
| **Claude Cowork** (local mode) | stdio | ✅ Plug-and-play |
| **Claude Cowork** (remote mode) | HTTP | ✅ Paste URL into Settings → Connectors |
| **claude.ai web** (Custom Connectors) | HTTP | ✅ Needs HTTPS endpoint + bearer token |
| **Cursor** | stdio | ✅ Plug-and-play |
| **VS Code MCP / Continue / Zed** | stdio | ✅ Plug-and-play |

## Quickstart (local stdio)

```sh
git clone https://github.com/silly-geese/estonian-mcp.git
cd estonian-mcp
uv sync
uv run python tests/test_smoke.py   # verify
```

Then wire it into your client (snippets below). Python 3.10–3.13
required (Vabamorf is a C++ extension, only prebuilt wheels work).

### Claude Code

```sh
claude mcp add estnltk -- /absolute/path/to/uv \
  --directory /absolute/path/to/estonian-mcp \
  run python server.py
```

### Claude Desktop / Claude Cowork (local mode)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or the equivalent on your platform:

```json
{
  "mcpServers": {
    "estnltk": {
      "command": "/absolute/path/to/uv",
      "args": [
        "--directory", "/absolute/path/to/estonian-mcp",
        "run", "python", "server.py"
      ]
    }
  }
}
```

Restart the app.

### Cursor

```json
{
  "mcpServers": {
    "estnltk": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/estonian-mcp", "run", "python", "server.py"]
    }
  }
}
```

## Run as a remote server

Use this when you want to plug the server into **claude.ai web**,
**Claude Cowork (remote mode)**, or share one instance across a team.
The HTTP transport requires a bearer token; the server refuses to start
without one.

### Option 1: Smithery (auto-host)

[Smithery](https://smithery.ai) builds + hosts the Docker image for
you. After [connecting your fork](https://smithery.ai/docs/build) and
deploying, install in any client with a single command. The repo
already contains `smithery.yaml` — Smithery picks it up automatically.

Users will be prompted for an `apiKey` value at install time, which is
the bearer token you set in your server's `ESTNLTK_MCP_AUTH_TOKEN`.

### Option 2: Fly.io (self-host on your domain)

```sh
fly auth login
fly apps create my-estonian-mcp                # pick a name
fly secrets set ESTNLTK_MCP_AUTH_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
fly deploy
```

Your endpoint is `https://my-estonian-mcp.fly.dev/mcp`. Health probe
is at `/health` and is unauthenticated. Auto-stops to zero when idle
to keep cost ~free; first request after idle has a ~5 s cold start.

### Option 3: Any container host

The included `Dockerfile` is platform-neutral. Build and run anywhere:

```sh
docker build -t estonian-mcp .
docker run --rm -p 8081:8081 \
  -e ESTNLTK_MCP_AUTH_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  estonian-mcp
```

### Wire a remote server into claude.ai web

**Settings → Connectors → Add custom connector.** Paste:

- **URL:** `https://your-host.example.com/mcp`
- **Authentication:** `Bearer <your token>`

### Wire a remote server into Claude Cowork

**Settings → Connectors → Add custom connector** in the Cowork app.
Same URL + bearer-token format as claude.ai web.

## How to prompt it

Once wired up, the tools appear in your client's tool list. Most prompts
don't need to mention them by name — the model picks the right tool.
Patterns that work well:

```
Proofread this Estonian email and use the estnltk spell_check tool
to verify any words you're unsure about: <text>
```

```
Lemmatize this Estonian paragraph using the estnltk MCP, then translate
the lemmas to English so I can study vocabulary: <text>
```

```
Analyze the morphology of this sentence with estnltk and explain the
case markings to me: "Tallinnas elavad eestlased räägivad eesti keelt."
```

```
Use estnltk's named_entities tool to extract people and places from
this Estonian news article, then summarize.
```

The model calls the tool, gets authoritative output, and bases its
response on that — no more hallucinated lemmas or invented case forms.

## Security

- **stdio mode**: pure local subprocess. No network egress, no shell
  exec, no fs writes, no telemetry.
- **HTTP mode**: requires `ESTNLTK_MCP_AUTH_TOKEN` (≥16 chars), refuses
  to start without it. Bearer-token auth on every request, constant-time
  comparison, per-token rate limit (60/min default), public `/health`
  but everything else 401s without a valid token. No request logging,
  no token logging.
- **Inputs**: 100 KB cap per text tool, 200 chars for `syllabify`.
  Oversized inputs return a structured error rather than hanging.
- **Supply chain**: deps pinned + hashed in `uv.lock`. Dependabot
  watches pip + GitHub Actions weekly. CI runs the smoke test on
  Python 3.11 and 3.13 on every push.

Full threat model and disclosure path: [SECURITY.md](SECURITY.md).

## Notes

- All EstNLTK models (morph, NER, spell-check) ship inside the wheel —
  no runtime downloads.
- Heavy neural taggers (`estnltk_neural`, BERT-based NER) are
  intentionally not pulled in; this server stays lean and fast.
- First call after server start incurs a one-time tag-layer load
  (~1–2 s). Subsequent calls are millisecond-scale.

## License

[Apache-2.0](LICENSE). EstNLTK itself is dual-licensed GPL-2.0 OR
Apache-2.0; we use it under the Apache-2.0 option. The bundled Vabamorf
analyzer is LGPL-2.1 with a separate commercial-use license — see
[NOTICE](NOTICE) for attribution and license obligations when
redistributing.
