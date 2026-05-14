# Terms of service

These are the terms of service for the **estonian-mcp** service
operated by Silly Geese Solutions at `https://estonian-mcp.fly.dev`.
See also the [privacy policy](PRIVACY.md) and the source-code
[license](LICENSE).

## Acceptance

By calling the service, you accept these terms. If you don't accept
them, don't call it. These terms govern use of the hosted service;
re-use of the source code is governed by [Apache-2.0](LICENSE).

## What the service is

A public Model Context Protocol server exposing Estonian NLP tools
(tokenisation, lemmatisation, POS tagging, morphological analysis,
spell-check, syllabification, named-entity recognition, WordNet
synonyms, fastText related-words, register classification). The
service runs on Fly.io in Amsterdam. The MCP endpoint is
`https://estonian-mcp.fly.dev/mcp`.

## Acceptable use

Anything legal and not abusive. Concretely permitted: integration
with AI agents, language learning, content drafting and proofreading,
research, education, and personal use.

Not permitted:

- Attempting to circumvent the per-IP rate limit via IP rotation,
  distributed traffic, or similar evasion.
- Using the service to facilitate activity that's illegal in your
  jurisdiction.
- Reselling access to this specific hosted endpoint as a paid
  product (the source code is Apache-2.0 — fork and host your own
  instance commercially if you want).
- Sustained automated load that materially impacts availability for
  other users; if you have a use case requiring high throughput, run
  your own deployment instead.

We may rate-limit, block, or terminate access from any IP that abuses
the service. Abuse determination is at our discretion.

## No warranty, no SLA

The service is provided **as is**, without warranty of any kind,
express or implied. We make no guarantee of uptime, accuracy,
completeness, fitness for any particular purpose, or
non-infringement. The service may be paused, modified, rate-limited,
or discontinued at any time without notice.

NLP outputs are informational and may contain errors. Estonian
linguistic analysis is hard; even the best tools occasionally
mis-segment compounds, mis-tag ambiguous forms, or miss neologisms
in their dictionaries. Do not rely on this service for any decision
where errors are materially costly without independent verification.

## Liability

To the maximum extent permitted by law, Silly Geese Solutions and
its contributors disclaim all liability for any damages — direct,
indirect, incidental, consequential, or otherwise — arising from
use of the service. Use is at your own risk.

## Data handling

See [PRIVACY.md](PRIVACY.md). Brief summary: we don't log request
bodies, we don't store tool inputs or outputs beyond the duration of
a single request, and we don't share anything with third parties.

## Output ownership

NLP tool outputs (lemmas, morphological analyses, syllable
breakdowns, synonym lists, etc.) are derived from open-source
linguistic data shipped inside the Docker image (EstNLTK, WordNet,
fastText — see [NOTICE](NOTICE) for full attribution). To the extent
any output may be subject to copyright (debatable for raw linguistic
analysis), Silly Geese Solutions does not claim ownership of your
generated outputs. You may use them for any purpose, subject to the
upstream model licences (CC-BY-SA-3.0 for fastText vectors; see
NOTICE).

## Changes

These terms may be updated. Substantive changes appear in git
history at `https://github.com/silly-geese/estonian-mcp`. The latest
version is always at
`https://github.com/silly-geese/estonian-mcp/blob/master/TERMS.md`.

## Contact

Operational issues, abuse reports, or questions:
[GitHub Issues](https://github.com/silly-geese/estonian-mcp/issues)
or `annamaria@sillygeese.co`. Security disclosures: see
[SECURITY.md](SECURITY.md).

Last updated: 2026-05-14.
