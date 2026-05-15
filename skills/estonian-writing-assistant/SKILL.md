---
name: estonian-writing-assistant
description: "Use this skill whenever the user is writing, editing, proofreading, or studying Estonian text and the estonian-mcp connector is available — even if they don't explicitly ask. Covers proofreading (spell_check first, then verify lemma and case forms via lemmatize and analyze_morphology on uncertain words), tone-aware rewrites (classify_register before and after edits to confirm the register matches the audience), vocabulary diversification (synonyms from WordNet for same-meaning swaps; find_related_words via fastText for adjacent concepts in marketing copy), morphology study, compound splits, syllabification, and named-entity extraction from Estonian news or documents. Core principle — never invent Estonian lemmas, case endings, conjugations, or spellings; always verify with the MCP tools, because models routinely hallucinate Estonian inflections. Documents tool quirks inline (fastText antonym-near-neighbours, polysemy, Vabamorf gaps on neologisms, synonyms-vs-related-words decision rule) and anti-patterns."
---

# Estonian writing assistant

Estonian has 14 cases, vowel harmony in compounds, productive subword
morphology, and a quantity-grade system that's easy to get wrong.
Models that haven't been heavily fine-tuned on Estonian will
confidently hallucinate plausible-looking but incorrect inflections.
This skill exists to prevent that: use the **estonian-mcp** tools as
ground truth for any factual claim about an Estonian word.

The hard rule, applied throughout this skill:

> Never assert an Estonian lemma, case form, conjugation, spelling, or
> compound split without verifying it with the MCP. If you're tempted
> to write "the partitive is X" or "this verb's stem is Y" from memory,
> stop and call the tool first.

## Tools at a glance

| Tool | When to reach for it |
| --- | --- |
| `spell_check` | First pass on any Estonian text the user wrote. Cheapest validation. |
| `lemmatize` | Get dictionary forms — for vocabulary study, deduping word stems, citation. |
| `analyze_morphology` | Full analysis with case form, root, ending, compound parts, ambiguity count, and a usage flag (`archaic` / `foreign` / `interjection` / `abbreviation` / `proper-noun`). The authoritative tool when discussing grammar. |
| `paradigm` | Generates the full inflection paradigm for a word — all 14 cases for nouns, ~30 forms for verbs. Use when the user asks "what's the X-case of Y" or wants to see every form. Don't try to recall paradigms from memory. |
| `pos_tag` | When you only need POS, e.g., filtering a list to nouns. |
| `tokenize` | Sentence + word boundaries. Useful before per-sentence operations. |
| `synonyms` | Same-meaning alternatives via Estonian WordNet. Returns synsets grouped by sense. |
| `find_related_words` | Semantically nearby words via fastText. Broader than synonyms — includes near-synonyms, related concepts, and (sometimes) antonyms. |
| `classify_register` | Heuristic formal/colloquial score with the markers that triggered it. Useful as a "did my edit drift in tone" check. |
| `check_capitalization` | Algustäheortograafia (initial-letter orthography) check per EKI Reeglid. Flags weekdays, months, nationalities, and language/culture adjectives wrongly capitalized mid-sentence. Run on every Estonian text you produce. |
| `check_compounds` | Liitsõnaõigekiri — flags common compound splits the model produces (`kooli maja` → `koolimaja`). Lexicon-based, phase 1. |
| `check_punctuation` | Kirjavahemärgid — flags missing commas before subordinating conjunctions (et, sest, kuna, kuid, vaid, nagu, …). Phase-1 scope: the comma-before-clause rule only. |
| `check_hyphenation` | Poolitamine — returns safe line-break positions for a word. Use only when typesetting/line-breaking matters; skip in normal writing. |
| `check_numbers` | Numbrite õigekirjutus — flags decimal-separator (period vs comma) and thousands-separator (comma vs space) violations. |
| `named_entities` | PER/LOC/ORG extraction. Useful for summarisation and content audit. |
| `syllabify` | Per-syllable breakdown with quantity + accent. Useful for slogan rhythm, song lyrics, or pronunciation guides. |

## Workflows

### 1. Proofread a draft

When the user asks you to proofread, copy-edit, or "check" Estonian
text:

1. Call `spell_check` on the full text. Note every word with
   `spelling: false`.
2. For each misspelled word, pick the top suggestion from
   `suggestions` if there's a clear best match; otherwise present the
   top 2–3 to the user.
3. Call `check_capitalization` on the same text. It catches
   AI-generated capitalization mistakes that `spell_check` cannot:
   `Eesti keel` (should be `eesti keel`), weekdays like
   `Esmaspäeval` (should be `esmaspäeval`), nationalities like
   `Eestlane` (should be `eestlane`), month names like `Jaanuaris`.
   Surface every issue with the `rule_estonian` label verbatim.
4. Call `lemmatize` on the corrected (or original, if no spell
   failures) text. Skim for any word whose lemma looks unexpected —
   that's often a sign the word is itself wrong (right spelling,
   wrong word).
5. For any word where the user asks "is this the right case here" or
   you suspect a wrong case form, call `analyze_morphology` and
   report the actual `form` (e.g., `sg p` = singular partitive).

**You should run `check_capitalization`, `check_compounds`,
`check_punctuation`, and `check_numbers` on every Estonian text you
yourself produce before sending it to the user**, not just on text
the user gives you. The four EKI-Reeglid checks together catch the
most common AI mistakes (wrongly-capitalized weekdays / language
adjectives, split compounds, missing commas before subordinating
conjunctions, wrong decimal/thousands separators). Each one is
cheap; run them all in parallel and surface every flag with the
`rule_estonian` label.

`check_hyphenation` is the odd one out — only call it if the user
explicitly cares about line breaks (slogans, signs, fixed-width
layouts). It's not part of the routine proofread pipeline.

Do not "fix" anything silently. List what you'd change and why, then
let the user accept or reject. Estonian grammar choices often depend
on register and stylistic intent that you can't infer from the
sentence alone.

### 2. Rewrite for a target register

When the user wants the text to feel more formal, more casual, or
more "on brand":

1. Call `classify_register` on the current text. Note the score and
   the matched markers.
2. If the current tier matches the target tier, the structural work
   is light — the user is asking for taste-level edits, not register
   shifts. Proceed with stylistic suggestions and call
   `classify_register` again on the proposed rewrite to verify you
   didn't drift.
3. If you need to shift register, look at the markers that triggered
   the current score. To make text **less formal**, find swaps for
   officialese verbs (`sätestama`, `tagama`, `kohaldama`,
   `käesolev`). To make it **more formal**, replace discourse
   particles (`noh`, `vot`, `kuule`, anglicisms like `okei`/`äge`).
4. For each marker you want to replace, use `synonyms` to find a
   real Estonian alternative at the target register. Don't invent.
5. Call `classify_register` on the rewrite to confirm the shift
   landed.

`classify_register` is a **phase-1 heuristic** — most newsletter
prose intentionally scores "neutral" because the markers it catches
are absent. Don't over-interpret a neutral score; it just means
"no obvious officialese or slang detected."

### 3. Break repetition in long-form copy

When the user complains a word repeats too much, or you notice
yourself overusing one verb:

1. Call `lemmatize` on the text to find which lemmas actually repeat
   (surface forms can differ while sharing a lemma).
2. For each over-used lemma, decide:
   - **Need a same-meaning swap?** Call `synonyms(lemma)` — returns
     WordNet synsets with strict-synonym lemmas, organised by word
     sense. Pick the synset that matches the intended meaning.
   - **Want adjacent concepts to enrich the rewrite?** Call
     `find_related_words(lemma)` — fastText's nearest neighbours,
     which include near-synonyms, related concepts, sometimes
     antonyms.
3. Use `analyze_morphology` to put the new lemma into the same case
   form as the original word being replaced — don't drop a nominative
   into a slot that requires partitive.

**Decision rule between the two:** if the user wants to "say the same
thing differently," start with `synonyms`. If they want "what else
belongs in this conceptual space" (richer marketing copy, brainstorm,
related products), use `find_related_words`.

### 4. Verify case forms and grammar claims

When the user asks "what case is X" or you're about to explain why
a form is what it is:

1. Call `analyze_morphology(text, all_analyses=True)` — pass
   `all_analyses=True` to see every possible analysis. Estonian
   words are often morphologically ambiguous; a single surface form
   like `riigid` could be plural nominative of `riik`.
2. Read the `form` field on each analysis. The notation is `<number>
   <case>` for nominals (`sg n` = singular nominative, `pl p` =
   plural partitive) or person/tense/mood for verbs.
3. The `root_tokens` field shows compound parts (`maailm` →
   `["maa", "ilm"]`). Cite this when explaining compounds; don't
   guess the split.
4. The `ending` field tells you the literal case ending; useful when
   the user wants to know how a paradigm works.

If a word has multiple analyses with different `form` values, that
means the surface form is genuinely ambiguous and the answer depends
on context. State that explicitly rather than picking one and acting
sure.

### 5. Study or teach Estonian

When the user is learning Estonian and wants to understand a
sentence or paragraph:

1. `tokenize` the text to get sentence and word boundaries.
2. `analyze_morphology` over each sentence — explain each word's
   lemma, POS, and case form.
3. For unknown words, `synonyms(lemma)` gives WordNet definition
   text in Estonian — pair it with your own translation.
4. For compounds, surface `root_tokens` so the learner sees how the
   word was built (`maailm` = `maa` "land/world" + `ilm` "weather/air").

### 6. Named entity extraction or content audit

For Estonian news, reports, or articles:

1. `named_entities(text)` returns PER/LOC/ORG with offsets.
2. Pair with `tokenize` if you also need the surrounding sentence
   for context summarisation.

## Quirks and edge cases you should expect

### `spell_check` misses neologisms and proper nouns

Vabamorf (the underlying spell-check) is the gold standard but its
lexicon doesn't cover every brand name, anglicism, or 2020s coinage.
If `spell_check` flags something the user clearly intended (e.g.,
their company name "Sillygeese"), treat it as a name and skip.

### `find_related_words` returns inflections of the input word

fastText's subword model gives high cosine similarity to other
inflected forms of the same lemma. Asking for `find_related_words`
on `kingitus` returns `kingituseks`, `kingitusi`, etc. before any new
word. To get only distinct concepts, post-process: lemmatize each
result and dedupe.

### `find_related_words` returns antonyms sometimes

Words that occur in similar contexts cluster together regardless of
polarity. `tark` (smart) can return `loll` (stupid). Read the
similarity score and the word itself — don't blindly suggest the top
match.

### `find_related_words` doesn't disambiguate polysemy

`lahe` means both "bay" (geography) and "cool" (colloquial). The
model returns whichever sense dominates the training corpus
(usually the geographic one for `lahe`). If the user is asking about
the colloquial sense, this tool won't help — use `synonyms` instead,
which returns the slang sense as a separate synset.

### `synonyms` returns multiple synsets per word

A polysemous word has one synset per meaning. Pick by reading the
`definition` field. Don't mix lemmas across synsets when proposing
swaps.

### `classify_register` is coarse

The classifier is lexicon-based. It flags obvious officialese
(`käesolev`, `vastavalt`, `sätestama`) and obvious colloquialisms
(`noh`, `kuule`, `vinge`). It will not catch register cues that live
in syntax (passive voice, address forms, sentence length). Use it as
a directional hint, not a verdict.

### `classify_register` returns the tier label in two languages

The response has both `tier` (English: `formal`, `neutral`,
`colloquial`, etc.) and `tier_estonian` (the correct Estonian
rendering: `formaalne`, `neutraalne`, `kõnekeelne`, …). When you
reply to the user in Estonian, quote `tier_estonian` verbatim
rather than translating `tier` yourself — the most common
mistranslation is `formalne` (wrong) instead of `formaalne`
(correct). This is the whole point of having the field.

### `syllabify` rejects multi-word input

It's strictly per-word. If the user wants syllabification of a
sentence, split first via `tokenize` and call `syllabify` per word.

## Anti-patterns

**Don't invent Estonian.** If you don't know a form and the MCP
tools are available, use them. If you genuinely don't know how a
word inflects and the tools haven't helped, say so. The user would
much rather hear "I'm not sure of the partitive form here" than
read a confident hallucination.

**Don't bypass `spell_check`** on user-provided text just because it
"looks fine." Estonian misspellings frequently produce words that
parse morphologically. `sõinn` looks plausible but is wrong; only
the spell-checker catches it.

**Don't pick the top fastText neighbour without inspecting** —
inflections and antonyms cluster high. Read the candidates.

**Don't change register silently.** If you're rewriting and the
register shifts, surface that explicitly: *"This rewrite is more
casual than the original. Want me to keep it formal?"*

**Don't use the surface form when you mean the lemma.** When the
user asks "what does this word mean," look up the lemma in
`synonyms`/dictionaries, not the inflected surface form.

## Calling pattern reminders

- Tools are read-only and idempotent. Call them as often as
  reasoning demands; they're cheap.
- Input cap is 100,000 characters per text tool, 200 chars for
  `syllabify`. For very long texts, paragraph-batch them and merge
  the results.
- All Estonian-language results are returned in UTF-8. Preserve the
  characters õ, ä, ö, ü, š, ž literally in your output — don't
  romanise them.

## How the user usually phrases requests

Use the skill on **any** of these patterns, plus reasonable
paraphrases:

- "Proofread this Estonian email/newsletter/draft…"
- "Is this case form correct?"
- "What's the lemma of X?"
- "Can you suggest a less repetitive way to say…"
- "Soften / formalise this Estonian paragraph for…"
- "Translate this Estonian sentence and explain the grammar"
- "Extract the people and places from this Estonian article"
- "Help me study Estonian morphology"
- "What words go with X in Estonian?"
- "How is this word built?" (compound questions)
- "Does this sound natural in Estonian?"

When the user pastes Estonian text without an explicit instruction,
default to: spell-check first, surface anything questionable, then
ask what they want to do with it.
