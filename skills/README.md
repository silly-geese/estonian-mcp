# Skills

Agent Skills are modular instruction packs that ship alongside the
`estonian-mcp` server. When a Claude client has both this MCP and
the matching skill installed, Claude reads the skill's instructions
at the start of relevant conversations and uses the tools in the
recommended patterns.

Skills here are independent of the MCP server code — they're
just markdown — and can be installed without redeploying anything.

## Available skills

| Skill | What it does |
| --- | --- |
| [`estonian-writing-assistant`](estonian-writing-assistant/SKILL.md) | Guides Claude through proofreading, register-aware rewriting, breaking repetition, and morphology study using the ten estonian-mcp tools. Documents tool quirks (Vabamorf misses on neologisms, fastText antonym-near-neighbours, polysemy) and anti-patterns (inventing case forms, bypassing spell_check). |

## Adding to the Anthropic Connectors Directory

Each skill is submitted via its GitHub URL on the Connectors
Directory submission form. The URL for the writing-assistant skill
is:

```
https://github.com/silly-geese/estonian-mcp/tree/master/skills/estonian-writing-assistant
```

## Authoring guidance

Anthropic's published guidance:
- [Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
- [Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- [Sample skills (anthropics/skills)](https://github.com/anthropics/skills)

In short: each skill is one directory with a `SKILL.md` file. The
frontmatter (`name`, `description`) must be present; the body holds
instructions Claude reads when the skill activates. Keep the body
under ~500 lines; reference external files via `references/`,
`scripts/`, or `assets/` subdirectories if you outgrow that.

## License

Skills inherit the repo's [Apache-2.0 license](../LICENSE).
