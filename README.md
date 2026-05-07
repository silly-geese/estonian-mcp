# estonian-mcp

> Claude is quite bad at Estonian, so this MCP/wrapper is here to fix that. Give it a shot.

[![CI](https://github.com/Unbelieva/estonian-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Unbelieva/estonian-mcp/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-stdio-7c3aed.svg)](https://modelcontextprotocol.io)

A small, local **Model Context Protocol** server that exposes
[EstNLTK](https://github.com/estnltk/estnltk) — the Estonian NLP toolkit — as
tools any LLM client can call in real time. Hand it Estonian text, get back
correct lemmas, morphology, POS tags, spell-check + suggestions, syllables,
and named entities.

If your AI agent has to draft, edit, or proofread Estonian, this wires in
ground truth so it stops guessing. Pure local, stdio-only, ~200 lines of
code you can read top-to-bottom.

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

| Client | Works? | How |
| --- | --- | --- |
| **Claude Code** (CLI) | ✅ | `claude mcp add estnltk -- uv --directory <path> run python server.py` |
| **Claude Desktop** (Mac/Win) | ✅ | Add to `claude_desktop_config.json` (snippet below) |
| **Cursor** | ✅ | `~/.cursor/mcp.json` with the same stdio command |
| **VS Code MCP / Continue / Zed** | ✅ | Any client supporting stdio MCP |
| **claude.ai** (regular web Claude) | ⚠️ | Web Claude only accepts **remote** MCP servers (HTTP/SSE). Use [`mcp-proxy`](https://github.com/sparfenyuk/mcp-proxy) to expose this stdio server over HTTPS, or self-host it. |
| **Claude for Work / Enterprise** | ⚠️ | Same as claude.ai — needs remote transport. |

## Install

EstNLTK requires Python 3.10–3.13 (Vabamorf is a C++ extension, only
prebuilt wheels work). This repo pins **3.13** via `.python-version`.

```sh
git clone https://github.com/Unbelieva/estonian-mcp.git
cd estonian-mcp
uv sync
```

(One-time ~250 MB of EstNLTK; no network calls at runtime after that.)

Verify:

```sh
uv run python tests/test_smoke.py
```

## Wire it into your client

### Claude Code

```sh
claude mcp add estnltk -- /absolute/path/to/uv \
  --directory /absolute/path/to/estonian-mcp \
  run python server.py
```

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

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

### claude.ai (web) via remote transport

Web Claude takes only HTTP/SSE MCP servers. Wrap this stdio server with
[`mcp-proxy`](https://github.com/sparfenyuk/mcp-proxy):

```sh
uvx mcp-proxy --port 8765 -- uv --directory /path/to/estonian-mcp run python server.py
```

then expose `http://localhost:8765/sse` over HTTPS (Cloudflare Tunnel,
ngrok, your own server) and add the URL as a Custom Connector in
claude.ai. Add auth at the proxy layer if exposing publicly.

## How to prompt it

Once wired up, the MCP tools appear in your client's tool list automatically.
Most prompts don't need to mention them by name — the model picks the right
tool from the user's request. A few patterns that work well:

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

The model will call the right tool, get authoritative output, and base
its response on that — so you stop seeing hallucinated lemmas or invented
case forms.

## Security

This server is **local stdio only** — no network egress, no shell exec,
no filesystem writes, no telemetry. Inputs are size-bounded (100 KB per
text tool, 200 chars for `syllabify`). Dependencies are pinned + hashed
in `uv.lock`; Dependabot watches for CVEs. The whole server is one file
you can audit in five minutes.

Full threat model and disclosure process: [SECURITY.md](SECURITY.md).

## Notes

- All models (morph, NER, spell-check) ship inside the EstNLTK wheel —
  no runtime downloads.
- Heavy neural taggers (`estnltk_neural`, BERT-based NER) are
  **intentionally not pulled in**; this server stays lean and fast.
- First call after server start incurs a one-time tag-layer load
  (~1–2 s). Subsequent calls are millisecond-scale.

## License

[Apache-2.0](LICENSE). EstNLTK itself is dual-licensed GPL-2.0 OR
Apache-2.0; we use it under the Apache-2.0 option. The bundled Vabamorf
analyzer is LGPL-2.1 with a separate commercial-use license — see
[NOTICE](NOTICE) for attribution and license obligations when
redistributing.
