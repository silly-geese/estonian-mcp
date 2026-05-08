# estonian-mcp

> Claude is quite bad at Estonian, so this MCP is here to fix that. Give it a shot.

[![CI](https://github.com/silly-geese/estonian-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/silly-geese/estonian-mcp/actions/workflows/ci.yml)
[![smithery badge](https://smithery.ai/badge/silly-geese/estonian-mcp)](https://smithery.ai/servers/silly-geese/estonian-mcp)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-stdio%20%2B%20HTTP-7c3aed.svg)](https://modelcontextprotocol.io)

A small **Model Context Protocol** server that exposes
[EstNLTK](https://github.com/estnltk/estnltk) — the Estonian NLP toolkit —
as tools any LLM client can call in real time. Hand it Estonian text,
get back correct lemmas, morphology, POS tags, spell-check + suggestions,
syllables, named entities, WordNet synonyms, and a register hint
(formal vs colloquial).

If your AI agent has to draft, edit, or proofread Estonian, this wires
in ground truth so it stops guessing on the mechanical layer
(spelling, case forms, conjugation) and gives it real Estonian
synonyms instead of inventing them.

**Three ways to use it:**

1. 👉 **Paste a URL into your Claude app** — the easiest path, no
   terminal, no install. See [Get started in 30 seconds](#-get-started-in-30-seconds-no-install) below.
2. **One-click on Smithery** — install from the
   [estonian-mcp listing](https://smithery.ai/servers/silly-geese/estonian-mcp).
3. **Self-host** — clone, run locally as stdio, or deploy your own
   container to Fly.io / any host. See [Self-host (advanced)](#self-host-advanced).

## What it does

| Tool | What it does |
| --- | --- |
| `tokenize(text)` | Split text into sentences and words |
| `analyze_morphology(text)` | Lemma, POS, form, root, ending, clitic, compound parts per word |
| `lemmatize(text)` | Just the dictionary form per word |
| `pos_tag(text)` | Just the part-of-speech tag per word |
| `spell_check(text)` | Spelling check + correction suggestions |
| `syllabify(word)` | Syllables with quantity + accent |
| `named_entities(text)` | People / places / organisations |
| `synonyms(word)` | Synsets from Estonian WordNet — synonymous lemmas + definition + examples per word sense |
| `classify_register(text)` | Coarse formal/colloquial register hint with matched markers (heuristic, phase 1) |

POS tag set: `S`=noun, `V`=verb, `A`=adj, `P`=pron, `D`=adv, `K`=adp,
`J`=conj, `N`=numeral, `I`=interj, `Y`=abbrev, `X`=foreign, `Z`=punct.

---

## ✨ Get started in 30 seconds (no install)

This section is for everyone — including if you've never opened a
terminal in your life. You'll be done before your tea is steeped.

The trick is that we run the server for you on the public internet at
`https://estonian-mcp.fly.dev/mcp`. You just need to tell your Claude
app to talk to it. Pick the app you use:

### In Claude Cowork

1. Open Cowork and click your profile / **Settings**.
2. Find **Connectors** in the sidebar.
3. Click **Add custom connector**.
4. Paste this URL into the URL field:
   ```
   https://estonian-mcp.fly.dev/mcp
   ```
5. Leave any "Authentication" / "API key" / "Bearer token" fields
   **empty**. The server is public — no token needed.
6. Click **Save** / **Connect**.
7. Done. Start a new chat and write in Estonian — proofread an
   email, study a paragraph, draft a reply. Claude will reach for
   the EstNLTK tools whenever it needs to verify spelling, lemmas,
   or morphology rather than guessing.

### In claude.ai (web Claude)

1. Click your profile in the bottom-left → **Settings**.
2. Find **Connectors** (sometimes called **Custom Integrations**).
3. Click **Add custom connector**.
4. Paste:
   ```
   https://estonian-mcp.fly.dev/mcp
   ```
5. Authentication: **none** (leave fields blank).
6. Save. The new tools appear in your tool tray.

### In Claude Desktop

If your Claude Desktop has a **Settings → Connectors** menu (newer
versions), follow the same three steps as Cowork above.

If it doesn't, you have an older Desktop that needs a JSON config
file edit — see [Self-host (advanced)](#self-host-advanced) for the
local-stdio path, which works on every version.

### Don't see your client here?

Any tool that supports MCP over HTTPS can connect — just point it at
`https://estonian-mcp.fly.dev/mcp` with no auth. If your client only
speaks stdio (Cursor, VS Code MCP, Continue, Zed, Claude Code), jump
to the local-install path in [Self-host](#self-host-advanced).

---

## 💡 Pro tip — teach Claude *your* Estonian alongside the MCP

This MCP gives Claude **correct linguistics**: real lemmas, real case
forms, real spelling. What it can't do is teach Claude **your voice** —
the register, idioms, and tone you actually want when writing.

You handle the voice; the MCP handles the correctness. Layer them.

A few things to add to your Claude project / custom instructions /
system prompt to get this right:

- **Set the register.** *"Always reply in formal officialese Estonian
  for legal and government topics, and in conversational Tallinn
  speech for chat replies. Never mix the two in one message."*
- **Pin the dialect / region.** *"I'm from Tartu — prefer southern
  Estonian phrasings where there's a choice (e.g. 'kus sa lähed'
  rather than 'kuhu sa lähed' for casual speech)."*
- **Show your tone with examples.** Paste 3–4 short paragraphs of
  your own writing into the project instructions and ask Claude to
  match that voice. Real examples beat any abstract description.
- **Anchor common mistakes.** *"You always confuse `kasutama` (to use)
  with `käsitlema` (to handle / to deal with). Double-check those
  with the lemmatize tool before sending."*
- **Direct the MCP explicitly when it matters.** *"Before sending any
  Estonian email, run spell_check on every word. Show me misspelled
  words with suggestions before drafting."*
- **Use `classify_register` as a sanity check.** *"After drafting,
  run classify_register on the final text and warn me if it lands
  in 'formal' or 'colloquial' when I asked for the opposite."* The
  classifier is coarse but reliably catches drift into officialese
  (`käesolev`, `vastavalt`, `sätestama`) or slang (`mõnus`, `vinge`,
  `kuule`).
- **Use `synonyms` to break repetition.** *"This newsletter uses
  `kasutama` four times. Look up synonyms via the MCP and suggest
  natural-sounding swaps."* You'll get real Estonian alternatives
  with definitions, not invented ones.

The MCP catches misspelled words and invented case forms; your
prompt drives the style. Together they make Claude actually useful
for writing in Estonian, not just plausible-looking.

---

## How to prompt it once it's connected

Most prompts don't need to mention the tools by name — Claude picks
the right one. A few patterns that work especially well:

```
Proofread this Estonian email and use spell_check on any words
you're unsure about: <text>
```

```
Lemmatize this Estonian paragraph, then translate the lemmas to
English so I can study vocabulary: <text>
```

```
Analyze the morphology of this sentence and explain the case
markings: "Tallinnas elavad eestlased räägivad eesti keelt."
```

```
Extract the people and places from this Estonian news article,
then summarise in one paragraph.
```

```
This Estonian draft uses "kasutama" three times — look up synonyms
via the MCP and rewrite each occurrence with a natural-sounding
alternative that preserves the meaning.
```

```
Classify the register of this draft. If it scores formal, soften
it for a casual newsletter audience. If it scores colloquial,
tighten it for a B2B email.
```

The model calls the tool, gets authoritative output, and bases its
response on that — no more hallucinated lemmas or invented case forms.

---

## All clients at a glance

| Client | No-install path | Local-install path |
| --- | --- | --- |
| **Claude Cowork** | ✅ Paste URL | ✅ stdio via JSON |
| **Claude Desktop** | ✅ Paste URL (newer) | ✅ stdio via JSON |
| **claude.ai web** | ✅ Paste URL | — |
| **Claude Code** (CLI) | — | ✅ `claude mcp add ...` |
| **Cursor** | — | ✅ stdio via JSON |
| **VS Code MCP / Continue / Zed** | — | ✅ stdio via JSON |

"No-install path" = paste `https://estonian-mcp.fly.dev/mcp` in the
client's Connectors UI. "Local-install path" = clone the repo and
point the client at `python server.py`.

---

## Self-host (advanced)

The hosted instance is convenient, but if you'd rather run your own
(privacy, latency, custom auth, offline use), the same one-file
server works locally and as a container.

### Run locally as stdio (zero network)

EstNLTK requires Python 3.10–3.13.

```sh
git clone https://github.com/silly-geese/estonian-mcp.git
cd estonian-mcp
uv sync
uv run python tests/test_smoke.py     # verify
```

Then wire it into your client.

**Claude Code:**
```sh
claude mcp add estnltk -- /absolute/path/to/uv \
  --directory /absolute/path/to/estonian-mcp \
  run python server.py
```

**Claude Desktop / Cowork (local mode)** — edit
`~/Library/Application Support/Claude/claude_desktop_config.json`:
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

**Cursor** — same JSON shape in `~/.cursor/mcp.json`.

### Run as a remote server (HTTP)

The same `server.py` speaks `streamable-http` over the network.
Two auth postures:

- **Public mode** (`ESTNLTK_MCP_PUBLIC_MODE=1`) — no bearer token,
  per-IP rate limit (default 120/min). This is how the silly-geese
  hosted instance runs.
- **Bearer mode** (default) — every request must carry
  `Authorization: Bearer <token>` (or Smithery's `?config=<base64>`);
  per-token rate limit. Refuses to start without
  `ESTNLTK_MCP_AUTH_TOKEN` ≥16 chars.

**Fly.io public deployment** (matches silly-geese):
```sh
fly auth login
fly apps create my-estonian-mcp
fly deploy
```
`fly.toml` already sets `ESTNLTK_MCP_PUBLIC_MODE=1`. Endpoint:
`https://my-estonian-mcp.fly.dev/mcp`.

**Fly.io with bearer auth** — remove
`ESTNLTK_MCP_PUBLIC_MODE` from `fly.toml`'s `[env]` block, then:
```sh
fly secrets set ESTNLTK_MCP_AUTH_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
fly deploy
```

**Generic Docker** (any container host):
```sh
# Public
docker run -p 8081:8081 -e ESTNLTK_MCP_PUBLIC_MODE=1 \
  ghcr.io/silly-geese/estonian-mcp     # or build from source

# Bearer
docker run -p 8081:8081 \
  -e ESTNLTK_MCP_AUTH_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  ghcr.io/silly-geese/estonian-mcp
```

**Smithery** auto-builds from `smithery.yaml` and hosts the image
for you. Fork, [connect on Smithery](https://smithery.ai/docs/build),
deploy. The shipped `configSchema` is empty (one-click install)
because the deployment runs in public mode; flip it back if you fork
to a bearer-mode setup.

---

## Security

- **stdio mode**: pure local subprocess. No network egress, no shell
  exec, no fs writes, no telemetry.
- **HTTP / public mode**: no auth required (intentional for the free
  public service). Per-IP rate limit (120/min default). Same hardening
  as bearer mode: no shell exec, no fs writes, no telemetry,
  size-bounded inputs.
- **HTTP / bearer mode**: `ESTNLTK_MCP_AUTH_TOKEN` (≥16 chars)
  required, server refuses to start without it. Bearer auth on every
  request, constant-time comparison, per-token rate limit (60/min).
- **Common to all HTTP**: `/health` is the only unauthenticated path.
  No request or token logging. `proxy_headers=True` so client IPs
  reflect the originator, not the platform's edge.
- **Inputs**: 100 KB cap per text tool, 200 chars for `syllabify`.
  Oversized inputs return a structured error rather than hanging.
- **Supply chain**: deps pinned + hashed in `uv.lock`. Dependabot
  watches pip + GitHub Actions weekly. CI runs smoke + HTTP tests +
  Docker build/boot on Python 3.11 and 3.13 on every push.

Full threat model and disclosure path: [SECURITY.md](SECURITY.md).

---

## Notes

- Most EstNLTK models (morph, NER, spell-check) ship inside the
  wheel — no runtime downloads.
- WordNet is a separate ~26 MB resource (used by `synonyms`); the
  Docker image pre-downloads it at build time so the first call
  doesn't pause to fetch it.
- Heavy neural taggers (`estnltk_neural`, BERT-based NER) are
  intentionally not pulled in; this server stays lean and fast.
- First call after server start incurs a one-time tag-layer load
  (~1–2 s). Subsequent calls are millisecond-scale.
- The hosted Fly instance scales to zero when idle; the first request
  after a quiet period takes ~5 s, then everything is fast again.

## License

[Apache-2.0](LICENSE). EstNLTK itself is dual-licensed GPL-2.0 OR
Apache-2.0; we use it under the Apache-2.0 option. The bundled Vabamorf
analyzer is LGPL-2.1 with a separate commercial-use license — see
[NOTICE](NOTICE) for attribution and license obligations when
redistributing.
