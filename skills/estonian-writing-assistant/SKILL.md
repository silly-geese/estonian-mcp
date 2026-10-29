---
name: estonian-writing-assistant
description: "Use this skill whenever the user is writing, editing, proofreading, or studying Estonian text and the estonian-mcp connector is available, even if they don't explicitly ask. Covers writing or translating Estonian that reads native rather than translated from English (short sentences, Estonian structure, then check_translationese), proofreading (spell_check first, then verify lemma and case forms via lemmatize and analyze_morphology on uncertain words), tone-aware rewrites (classify_register before and after edits to confirm the register matches the audience), de-bureaucratising stiff Estonian prose (check_officialese for kantseliit in reports, academic and business writing, where check_legalese stays silent), keeping one term per referent across a long document (check_term_consistency), answering 'is this the right word here?' by reading each WordNet gloss's domain constraint rather than trusting ML-jargon familiarity, vocabulary diversification (synonyms from WordNet for same-meaning swaps; find_related_words via fastText for adjacent concepts in marketing copy), morphology study, compound splits, syllabification, and named-entity extraction from Estonian news or documents. Core principle: never invent Estonian lemmas, case endings, conjugations, or spellings; always verify with the MCP tools, because models routinely hallucinate Estonian inflections. Documents tool quirks inline (fastText antonym-near-neighbours, polysemy, Vabamorf gaps on neologisms, synonyms-vs-related-words decision rule) and anti-patterns."
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
| `lemmatize` | Get dictionary forms: for vocabulary study, deduping word stems, citation. |
| `analyze_morphology` | Full analysis with case form, root and ending, plus, where they apply, compound parts, an ambiguity count, an `indeclinable` flag and a usage flag (`archaic` / `foreign` / `interjection` / `abbreviation` / `proper-noun`). The authoritative tool when discussing grammar. |
| `paradigm` | Generates the full inflection paradigm for a word: all 14 cases for nouns plus the short illative where a word has one (majja beside majasse), ~39 forms for verbs. Proper nouns inflect too (Tallinn → Tallinnasse); a name Vabamorf lacks gets an empty table, never invented forms. Use when the user asks "what's the X-case of Y" or wants to see every form. Don't try to recall paradigms from memory. |
| `pos_tag` | When you only need POS, e.g., filtering a list to nouns. |
| `tokenize` | Sentence + word boundaries. Useful before per-sentence operations. |
| `synonyms` | Same-meaning alternatives via Estonian WordNet. Returns synsets grouped by sense. |
| `find_related_words` | Semantically nearby words via fastText. Broader than synonyms: includes near-synonyms, related concepts, and (sometimes) antonyms. |
| `classify_register` | Heuristic formal/colloquial score with markers + `consistency.is_mixed` flag for register-mixed text (formal + colloquial markers in the same draft = jarring). |
| `check_style` | Four style metrics in one call: lemma-aware repetition (catches 'kasutab' / 'kasutas' both under lemma 'kasutama'), Estonian passive-voice ratio (-takse/-ti/-tud family), sentence-length mean+stddev, hedging-word density. Use for newsletter / ad / email polish. |
| `check_redundancy` | Pleonasm / semantic-doubling check: flags `samuti ka` (also+also), `kõige optimaalsem` (most+optimal), and fixed redundant phrases. Run it before claiming a redundancy "the MCP can't catch": it catches the common ones. |
| `check_object_case` | Käändeõpetus heuristic. Catches the most common confidently-wrong Estonian: direct-object case after negation (must be partitive) and after partitive-only verbs (`armastama`, `vihkama`, `vajama`, …). Lexicon-based, no syntactic parser; only flags nouns AFTER the trigger so subject-noun false positives are minimal. An issue gives the noun's `lemma` and `form`, not a correction: take the partitive in the number `form` shows from `paradigm`. A negated sentence containing `olema` (`pole näinud`) is not checked for negation. It also checks verb government: `teavitama` / `informeerima` take the person in the partitive (`teavitas politseid`), `teatama` takes the recipient in the allative (`teatas politseile`), so `teavitas politseile` and `Teatame Teid` are flagged with both fixes in `suggestion`. Recall is about half by design: an allative that belongs to a participle (`teavitada isikut talle seatud piirangust`) is correct and stays quiet. |
| `check_abbreviation_hyphenation` | Lühendiortograafia: flags an ending that starts with l written straight onto an all-capital abbreviation (`OÜle` → `OÜ-le`, `ÜROlt` → `ÜRO-lt`, `THIl` → `THI-l`), where EKI finds the hyphen clearer. EKI accepts every other hyphenless form (`ELi`, `ELis`, `APIga`, `MCPst`, `ASidel` are as correct as `EL-i`, `EL-is`, `API-ga`, `MCP-st`, `AS-idel`), so do not "fix" them: keep the text's own choice. A letter whose name ends in a consonant takes the stem vowel, so `ASle` is misinflected and the suggestion is `AS-ile`. Any all-capital stem is checked, known or not; a mixed-case one (`SaaSi`, `PhDga`) and a chloride formula (`HCl`, `NH4Cl`) are not. |
| `check_compound_familiarity` | Attestation check for each compound noun: is the lemma in the fastText corpus vocabulary (`in_vocab`), in Estonian WordNet (`in_wordnet`), or a legal term of art? **Read `attested` first, and read `attested: false` for what it is**: outside a 100K-word vocabulary, which is the normal state for correct specialist vocabulary. `lisakäive`, `klikkimismäär`, `koolitoit`, `rehvivahetus` are all unattested here and all ordinary Estonian, so unattested is NEVER on its own a reason to rewrite. `is_suspect` is narrow: unattested AND the nearest neighbour is a real word AND that similarity is still under 0.55 (`mõtteliin`, 0.536). Recall is low on purpose: a coinage scoring inside the band real vocabulary occupies is missed rather than bought at the price of flagging good Estonian, so `toortõlkeoht` (0.571) passes. `neighbour_quality.signal: none` means the nearest neighbour is a scrape-artifact token, the score means nothing, and no verdict is offered. Note the other limit too: a compound that is merely *stilted* rather than invented (`teadusandmestik`) passes, because similarity can't judge register, so use `check_officialese` for that. Run on any Estonian compound you yourself produced (rather than verbatim user input). |
| `check_domain_terms` | For Estonian written about an organisation's own systems. Reports the compounds that are unattested AND not on the organisation's glossary or identifier list. That says only that the word is not the organisation's: it may be a name you coined, or ordinary Estonian the lexicons lack (`vagunisõit` is common). Where it names something the organisation has a word for, or something with an identifier, use theirs. When there are glossary terms, each comes with up to three of them **spelled alike** (`suggestions`): look-alikes, not synonyms, so replace the compound only when a suggestion names the same thing. `contains_glossary_term` marks a compound built on a glossary term (`vagunireisitabel` on `vagunireis`), which is ordinary Estonian and must not be shortened to the term. The hosted server has no glossary of its own, so pass the organisation's terms in `glossary` (up to 1,000). With no glossary at all it is only the unattested list, which is never on its own a reason to rewrite. |
| `check_officialese` | Kantseliit check for **non-legal** prose: reports, academic writing, business copy, grant/R&D paperwork. Use this, not `check_legalese`, for anything that isn't a statute or contract: `check_legalese`'s lexicon and 34-word gate are tuned for legislation and return nothing on report officialese. Gives nominalisation density with the verb to swap in (`hindamine` → `hindama`), correctly-counted umbisikuline tegumood, clause stacking, Estonian-calibrated sentence length, and admin filler. Also `läbi viima` in every form with a verb in the same form (`viidi läbi` → `korraldati / tehti`, `pidama` for a meeting), translated letter phrases (`calque-phrase`: `Palun leidke manuses`, `ärge kõhelge ... võtmast`, `kiri leiab teid hästi`, `tänan teid teie ... eest`, `vaatan edasi koostööle`, `sealhulgas, kuid mitte ainult`, `antud juhul`) and the request `Küsin täpsemalt, kas …` (`kusin-tapsemalt` → `Palun täpsustage, …` / `Sooviksin täpsemalt teada, …`). All of these are style hints: say so when you pass them on. |
| `check_translationese` | English sentence structure and English habits of form that make Estonian read as translated: `teie andmed` where the subject's own thing is `oma andmed`, `lähtume me`, a third of the sentences opening with `Ma`, `Kui ..., me saadame` (the verb comes first after a fronted clause), `saab olema`, `ettepanek võtmaks`, `Tagamaks`, `Mitte leides`, `tänu` with a bad cause, `minu arvamusel`, a letter's `Hea Mari,` / `Tere Mari` / `Lugupidamisega,`, the serial comma (`teed, ja`), title case in headings. Run it on every Estonian text you write or translate. `maks-purpose` and `subject-pronoun` are style hints. |
| `check_term_consistency` | One referent, one term. Run on any document longer than a couple of paragraphs: catches `andmestik` in §1, `teadusandmestik` in §2, `pildiandmestik` in §3. Reports per-variant counts so you can standardise on the dominant one. Pass the organisation's terms in `glossary` when you have them (up to 1,000; the hosted server has none of its own): when exactly one variant in a group is listed, the group names it `preferred`, so standardise on `preferred` when present and otherwise on `dominant`. Read each group before rewriting: some are genuinely distinct concepts. |
| `check_capitalization` | Algustäheortograafia (initial-letter orthography) check per EKI Reeglid. Flags weekdays, months, nationalities, and language/culture adjectives wrongly capitalized mid-sentence. A hit at the start of a line, list item or quotation, or inside a proper name (`Eesti Keele Instituut`), is usually false. Run on every Estonian text you produce. |
| `check_compounds` | Liitsõnaõigekiri: flags common compound splits the model produces (`kooli maja` → `koolimaja`). Lexicon-based, phase 1. |
| `check_punctuation` | Kirjavahemärgid: flags missing commas before subordinating conjunctions (et, sest, kuna, kuid, vaid, nagu, …). The comma-before-clause rule only. A comma before a two-part connective (`nii et`, `selleks et`) passes. With none, `ilma et` and `olgugi et` get it before the pair; the others get it before `et`, which suits manner (`Tegin nii, et kõik said aru`) but not result (`Sadas vihma, nii et jäime koju`). `nagu` is flagged only before a verb, so `tugev nagu karu` passes, but `Kassid nagu kõik koduloomad vajavad hoolitsust` and `punane nagu tuli` still flag, and `nagu` meaning näiteks (`Riigid, nagu Soome ja Rootsi, ...`) is never flagged: give those a second look. `comma-after-subordinate-clause` flags the comma that closes an embedded clause before the main verb (`Mees, kes seal seisis oli minu isa` → `, oli`); the comma belongs there at the latest and may belong earlier. `comma-before-ja-after-subordinate-clause` flags `ja` after a kes/mis clause when the verb after `ja` changes person back to the main clause's (`Juhtisin projekti, mis kestis kaks aastat ja koolitasin …` → `, ja`). EKI keeps that comma (reegel 44, märkus 2), so never delete a comma before `ja` that closes a clause. |
| `check_hyphenation` | Poolitamine: returns line-break positions for a word; compound seams are among them but not singled out, so pick the seam yourself. Use only when typesetting/line-breaking matters; skip in normal writing. |
| `check_numbers` | Numbrite õigekirjutus: flags decimal-separator (period vs comma) and thousands-separator (comma vs space) violations. It skips times after `kell`, time ranges, dates (`14.05`), clause, section and version numbers (`4.13. Tellija`, `punkt 3.2`, `Python 3.12`). It flags `1,250` as a thousands separator with the suggestion `1250` (EKI writes four digits together and groups from five: `12 500`); if you meant the decimal 1,25, keep it. A time without `kell` mid-sentence (`väljub 8.47 ja`) is still flagged, and a date-shaped decimal with no unit (`1.05 ja`) is skipped, so judge a hit or a miss next to a time or a date yourself. `approximate-tilde`: a tilde used as "about" before a number (`~50 inimest`) gets `u 50`, or `Umbes 50` at a sentence start; EKI writes rounded numbers with `u` (no period), `umbes` or `ligikaudu` and keeps the tilde for variants (`mina ~ ma`). `ca 50` and `u. 50` are fine; leave them. A spaced `A ~ 5` after a word, `≈`, code, paths and `5~10` are not flagged. `month-year-digits`: a month and year in digits (`09.2016`) is a date, and EKI finds the month in words clearer: `sept 2016` (the abbreviation needs no case ending: `alates sept 2016`; a full name does: `alates märtsist 2019`). `range-spacing`: no spaces around the dash of a range whose ends contain none (`2020 - 2024` → `2020-2024`, `5 - 10 päeva` → `5-10`); typeset EKI text uses the mõttekriips there, and a hyphen strictly means "ja" between consecutive numbers (`14-15 eurot`). Keep the spaces when an end contains one (`9000 - 10 000`, `aprill 2008 - august 2009`); write ordinal ranges as `2015.-2019. aastal` and seasons as `2019/2020`. `percent-sign-suffix`: `20%-line` → `20%` or `20-protsendiline`; case endings (`10%-st`) are fine. |
| `named_entities` | PER/LOC/ORG extraction. Useful for summarisation and content audit. |
| `syllabify` | Per-syllable breakdown with quantity + accent. Useful for slogan rhythm, song lyrics, or pronunciation guides. |

## Workflows

### 0. Write or translate into Estonian

Most "worse Estonian" is English structure, not wrong words: the
possessives, pronouns, word order, light verbs and punctuation of the
English it was thought in. Write from the meaning, then edit as an
Estonian editor would.

1. **Compose, don't translate.** Take the meaning and say it the Estonian
   way: the person in the verb ending (`Loodan`, not `Ma loodan`), `oma`
   for the subject's own thing, the verb right after a fronted clause
   (`Kui tellimus on kinnitatud, saadame arve`), plain verbs for `-mine`
   nouns and light verbs. Estonian says
   the same in about a quarter fewer words than English, so a version as
   long as its English source still carries English structure.
2. **Keep it short outside documents.** Messages, replies, e-mail, UI and
   marketing copy: 10 to 15 words a sentence, one idea each, two to four
   sentences a paragraph, the purpose first. No filler, hype, small talk
   or English enthusiasm (`!` after every line). A letter still keeps its
   greeting, thanks and closing: a bare one reads as rude.
3. **Documents keep their weight.** Reports, contracts, policies and
   academic text keep their content, defined terms and fixed formulas:
   never vary a term for style. Still remove kantseliit
   (`check_officialese`, `check_legalese`).
4. **Edit lightly.** When the user gave the text, change only what is
   wrong or unclear: no synonym swaps for taste (`ja` → `ning`), no word
   moved unless its order is wrong, no rewrite longer than the original
   unless asked.
5. **Revise in a separate pass.** Reread the draft once as an Estonian
   editor, then run `check_translationese` with the four orthography
   checks below, and fix the hits that hold. A second, separate pass does
   more than any instruction to "sound natural".

Letters: `Tere, Mari!` or `Hea Mari!` (or no mark), never a comma after
the greeting, and the first sentence starts with a capital. The closing
takes no comma: `Lugupidamisega` / `Mari Tamm`. Ask directly, with
`palun` and the imperative. `Teie` with a capital addresses one reader
and is correct.

### 1. Proofread a draft

When the user asks you to proofread, copy-edit, or "check" Estonian
text:

1. Call `spell_check` on the full text. It lists only the misspelled
   words; an empty list means every word passed.
2. For each misspelled word, pick the top suggestion from
   `suggestions` (absent when Vabamorf has none) if there's a clear
   best match; otherwise present the
   top 2-3 to the user.
3. Call `check_capitalization` on the same text. It catches
   AI-generated capitalization mistakes that `spell_check` cannot:
   `Eesti keel` (should be `eesti keel`), weekdays like
   `Esmaspäeval` (should be `esmaspäeval`), nationalities like
   `Eestlane` (should be `eestlane`), month names like `Jaanuaris`.
   Surface every issue with the `rule_estonian` label verbatim.
4. Call `lemmatize` on the corrected (or original, if no spell
   failures) text. Skim for any word whose lemma looks unexpected:
   that's often a sign the word is itself wrong (right spelling,
   wrong word).
5. For any word where the user asks "is this the right case here" or
   you suspect a wrong case form, call `analyze_morphology` and
   report the actual `form` (e.g., `sg p` = singular partitive).

**You should run `check_translationese`, `check_capitalization`,
`check_compounds`, `check_punctuation`, and `check_numbers` on every
Estonian text you yourself produce before sending it to the user**, not
just on text the user gives you. Together they catch the most common AI
mistakes (English structure and letter punctuation, wrongly-capitalized
weekdays / language adjectives, split compounds, missing commas before
subordinating conjunctions, wrong decimal/thousands separators). Each one
is cheap; run them all in parallel, check each flag against its
sentence, and surface the ones that hold with the `rule_estonian` label.

`check_hyphenation` is the odd one out: only call it if the user
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
   is light: the user is asking for taste-level edits, not register
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

`classify_register` is a **heuristic**: most newsletter prose
intentionally scores "neutral" because the markers it catches are
absent. Don't over-interpret a neutral score; it just means "no obvious
officialese or slang detected." Its `structure` block adds umbisikuline
tegumood ratio and noun density, which is what stops dense report prose
from scoring neutral, but it only applies from 25 words up.

### 2b. Make bureaucratic Estonian readable

When the user says a text is heavy, stiff, "kantseliitlik", or asks you
to make it "inimlikum" / simpler to read, and it is **not** a statute
or contract:

1. Call `check_officialese`. Work the issues in this order, because
   this is the order in which they change how the text reads:
   - `impersonal-voice`: name the actor. `koguti` → `kogusime`. This
     is the single biggest lever in Estonian report prose.
   - `nominalisation`: each `-mine` noun comes back paired with the
     verb to use instead. `metoodika väljatöötamine` → `töötati välja
     metoodika`.
   - `clause-stacking` / `long-sentence`: split. Prefer chronological
     order: first what was done, then what came of it.
   - `officialese-filler` / `officialese-phrase` / `poolt-calque`:
     straight swaps, each with a suggestion.
2. Call `check_term_consistency` on the whole document and pick one
   term per group. Do this **before** rewriting, so the rewrite
   standardises rather than adding a fourth variant.
3. Re-run `check_officialese` on your rewrite. The metrics should move:
   impersonal ratio down, `-mine` per 100 words down, longest sentence
   down.

Hard rule for this workflow: **every number, percentage and factual
claim stays exactly as it was.** Simplifying must not strengthen or
weaken a claim: if the original says no general conclusion is drawn
from a result, that hedge survives the rewrite verbatim.

Use `check_legalese` instead when the text *is* legal: it protects
terms of art, which `check_officialese` does not.

### 2c. "Is this the right word here?"

Distinct from "give me a synonym", and the tools answer it differently.
A word can pass `spell_check`, be a valid compound, and still be wrong.

1. Call `synonyms` on the candidate and **read each `definition`**, not
   just the lemma list. Estonian glosses often carry a domain
   constraint that settles the question: `korpus` returns "kirjaliku või
   suulise teksti elektrooniline kogu", so a set of *images* is not a
   `korpus` however normal that sounds in ML jargon. `andmestik`
   carries no such constraint.
2. If the candidate is a compound **you** coined, call
   `check_compound_familiarity` and act on `attested`, not on the score:
   unattested plus a compound you invented on the spot means find an
   attested phrasing; unattested for a term the user or the domain
   already uses means nothing at all. Remember it clears well-formed but
   stilted compounds: `teadusandmestik` passes at 0.705 even though a
   native speaker reads it as artificial. When the text is about an
   organisation's own systems and you have its terms, call
   `check_domain_terms` with them in `glossary` instead: a reported
   compound, coined or ordinary, should become the organisation's term or
   the thing's identifier where it names the same thing, and otherwise
   stay.
3. For register fit, call `classify_register` or `check_officialese`.
4. When the user is a native speaker and corrects your Estonian, they
   are right about how it reads: naturalness, register, word order,
   what sounds translated. Take the correction and move on. The one
   exception is a term of art a statute fixes, or a rule EKI states in
   so many words (see "Rules intuition gets wrong" below). When a
   correction would change one of those, quote the section or page and
   let them choose.

### 3. Break repetition in long-form copy

When the user complains a word repeats too much, or you notice
yourself overusing one verb:

1. Call `lemmatize` on the text to find which lemmas actually repeat
   (surface forms can differ while sharing a lemma).
2. For each over-used lemma, decide:
   - **Need a same-meaning swap?** Call `synonyms(lemma)`: returns
     WordNet synsets with strict-synonym lemmas, organised by word
     sense. Pick the synset that matches the intended meaning.
   - **Want adjacent concepts to enrich the rewrite?** Call
     `find_related_words(lemma)`: fastText's nearest neighbours,
     which include near-synonyms, related concepts, sometimes
     antonyms.
3. Use `analyze_morphology` to put the new lemma into the same case
   form as the original word being replaced: don't drop a nominative
   into a slot that requires partitive.

**Decision rule between the two:** if the user wants to "say the same
thing differently," start with `synonyms`. If they want "what else
belongs in this conceptual space" (richer marketing copy, brainstorm,
related products), use `find_related_words`.

### 4. Verify case forms and grammar claims

When the user asks "what case is X" or you're about to explain why
a form is what it is:

1. Call `analyze_morphology(text, all_analyses=True)`: pass
   `all_analyses=True` to see every reading left after in-context
   disambiguation. Estonian words are often morphologically
   ambiguous; a single surface form like `riigid` could be plural
   nominative of `riik`. Context has already removed readings
   (`seal` has three out of context and comes back with one), so a
   single reading does not prove the word is unambiguous on its own.
2. Read the `form` field on each analysis. The notation is `<number>
   <case>` for nominals (`sg n` = singular nominative, `pl p` =
   plural partitive) or person/tense/mood for verbs.
3. The `root_tokens` field, present only for compounds, shows
   compound parts (`maailm` →
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
2. `analyze_morphology` over each sentence: explain each word's
   lemma, POS, and case form.
3. For unknown words, `synonyms(lemma)` gives WordNet definition
   text in Estonian: pair it with your own translation.
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
similarity score and the word itself: don't blindly suggest the top
match.

### `find_related_words` doesn't disambiguate polysemy

`lahe` means both "bay" (geography) and "cool" (colloquial). The
model returns whichever sense dominates the training corpus
(usually the geographic one for `lahe`). If the user is asking about
the colloquial sense, this tool won't help: use `synonyms` instead,
which returns the slang sense as a separate synset.

### `synonyms` returns multiple synsets per word

A polysemous word has one synset per meaning. Pick by reading the
`definition` field. Don't mix lemmas across synsets when proposing
swaps.

### `classify_register` is coarse

The classifier is lexicon-based. It flags obvious officialese
(`käesolev`, `vastavalt`, `sätestama`), academic/report vocabulary
(`aruandeperiood`, `valideerima`, `metoodika`) and obvious
colloquialisms (`noh`, `kuule`, `vinge`). Its `structure` block adds
two syntactic signals (umbisikuline tegumood ratio and noun density),
but only from 25 words up, and address forms and finer syntax still go
uncaught. Use it as a directional hint, not a verdict; for a full
kantseliit breakdown with per-issue suggestions, call
`check_officialese`.

### `classify_register` returns the tier label in two languages

The response has both `tier` (English: `formal`, `neutral`,
`colloquial`, etc.) and `tier_estonian` (the correct Estonian
rendering: `formaalne`, `neutraalne`, `kõnekeelne`, …). When you
reply to the user in Estonian, quote `tier_estonian` verbatim
rather than translating `tier` yourself: the most common
mistranslation is `formalne` (wrong) instead of `formaalne`
(correct). This is the whole point of having the field.

### `syllabify` rejects multi-word input

It's strictly per-word. If the user wants syllabification of a
sentence, split first via `tokenize` and call `syllabify` per word.

## Rules intuition gets wrong

Each of these is correct Estonian that a fluent writer or editor can
"fix" into an error, or an error that reads fine. Cite the source when
you apply one.

### Terms of art: check the statute

A word can be correct Estonian and still be the wrong term. In anti-money
laundering, payments, legal or deal text:

- **teavitama vs teatama.** `teavitama` (and `informeerima`) takes the
  person in the partitive: *keda + millest* (`teavitas politseid
  õnnetusest`, `Teavitame Teid muudatustest`). `teatama` takes the
  recipient in the allative: *kellele + millest* (`teatas politseile`,
  `Teatame Teile, et ...`). RahaPTS keeps them apart: reporting to the
  Financial Intelligence Unit is `kahtlastest tehingutest teatamine`
  (§ 32 lg 3; § 49 is the `teatamiskohustus`), and the tipping-off ban
  in § 51, headed `Teate konfidentsiaalsus`, forbids *teavitada* the
  client. `check_object_case` flags `teavitama` with an allative
  recipient.
- **AML internal rules.** RahaPTS § 14 calls them `protseduurireeglid`
  and `sisekontrollieeskiri`. `sisekord` is an institution's house
  rules (kodukord, sisekorraeeskiri), not the AML document.
- **Payments and crypto.** `e-raha asutus`, two words (makseasutuste ja
  e-raha asutuste seadus § 7), and `makseasutus` (§ 5). spell_check
  passes `e-rahaasutus`; do not use it. In RahaPTS the prefix
  `virtuaal-` meant crypto (`virtuaalvääring`), and since 1 July 2026
  the act says `krüptovara` and `krüptovarateenuse osutaja`; never use
  `virtuaal-` for e-money.
- **Lawyers.** `advokaat` is a protected title (advokatuuriseadus § 22
  lg 3: members of Eesti Advokatuur). For a law firm's attorneys on a
  deal, `advokaadid` is right; for anyone else with a law degree,
  `juristid`; `õigusnõustajad` is the neutral word.
- **Result or process.** `hinnang` is the opinion you rely on
  (`toetudes audiitori hinnangule`), `hindamine` the act of assessing. A
  written valuation is a `hindamisaruanne`; a statutory audit gives an
  `arvamus` in a `vandeaudiitori aruanne`.
- **Due diligence.** There is no settled Estonian term. Keep `due
  diligence` as a quoted foreign term, inflected with an apostrophe
  (`due diligence'i`), and explain it once. RahaPTS `hoolsusmeetmed`
  are customer due diligence measures, a different thing.
- **Section names.** `liikmesus` and `liikmelisus` are both correct, as
  are `täiendkoolitus` and `täienduskoolitus` (the statute says
  `täienduskoolitus`). Pick one per document and run
  `check_term_consistency`.
- `common_legal_usage` indexes five core codes only, so `found: false`
  for these AML and payment terms means not indexed, nothing more.

### The comma that closes a clause, also before ja

A subordinate clause inside a main clause takes a comma on both sides
(EKI õigekirja põhireeglid, reegel 44): `Projekt, mida juhtisin,
kestis kaks aastat.` The closing comma stays even before ja, ning, ega,
ehk, või when the main clause continues after the conjunction (reegel
44, märkus 2): `Juhtisin projekti, mis kestis kaks aastat, ja koolitasin
uusi töötajaid.`, `õpetajana, kes hindab kirjandeid, ja õpilasena,
keda hinnatakse`. "There is almost never a comma before ja" is true
of ja joining words or main clauses, and wrong here. No comma before ja
when two clauses describe the same antecedent (`majas, mille ehitas mu
isa ja milles on kolm tuba`) or when ja joins words inside the clause
(`firmaga, mis teeb koostööd ülikooli ja haiglaga`). `check_punctuation`
flags the missing closing comma before the main verb, and before a ja
whose verb is clearly the main clause's (a change of person); the
essive and other parallel-case versions it cannot see, so read every
`, kes ... ja X-na` yourself.

### First person, and how a request is phrased

A first-person document (a cover letter, an application, a personal
profile) keeps one person. If the text says
`Juhtisin`, `Ehitasin`, `Vastutan`, then `Elab Tartus ja õpetab
matemaatikat` is a pasted third-person line: make it `Elan Tartus ja
õpetan matemaatikat`. A bio written
wholly in the third person is a genre and stays as it is; verbless
nominal lines (`Finantsjuht, 2019-2022`) are fine in either. No tool
checks this: morphology cannot tell a dropped "I" from a company named
in the heading.

In a letter, `Küsin täpsemalt, kas ...` reads as translated:
`täpsemalt` assumes an earlier, broader question, and the present
`küsin` narrates the asking instead of making the request. Write
`Palun täpsustage, kas ...` or `Sooviksin täpsemalt teada, kas ...`
(plural: `Palume täpsustada`, `Sooviksime täpsemalt teada`). Plain
`Küsin, kas ...` is natural, and so is `Küsin täpsemalt järgmisel
kohtumisel` (a plan). `check_officialese` flags only the request form.

The same tool flags other translated letter formulas (`calque-phrase`):
`Palun leidke manuses` → `Manuses on ...` (but `Hinnapakkumise leiate
manusest` is fine), `ärge kõhelge ühendust võtmast` → `võtke julgelt
ühendust`, `see kiri leiab teid hästi` (drop it), `tänan teid teie ...
eest` → `tänan teid ... eest`, `vaatan edasi koostööle` → `ootan huviga
koostööd`, `sealhulgas, kuid mitte ainult` → `sealhulgas`.

### Accepted Estonian that looks translated

Do not "fix" these: ÕS 2025 or the EKI 2026 dictionary accepts them, and
`check_translationese` leaves them alone. `aset leidma`, `rolli
mängima`, `mis puudutab`, `X aastat vana`, `rohkem kui`, `päeva lõpuks`
(ultimately), `kursis hoidma`, `hea meel` + da-infinitive, `teiste
sõnadega`, `läbi` for a means, `täna` for nowadays, `erinev` for various.
A pronoun subject after an adverb is normal (`Eile ma läksin poodi`), and
the comma after a fronted des phrase is required (`Kasutades seda
tööriista, säästad aega`).

### Light verbs and -mine nouns

`läbi viima` and `läbiviimine` are a style matter, not an error (EKI
does not normalise them), but they are the commonest kantseliit in
reports. Choose the verb by the object: a meeting, seminar, conference
or appraisal interview is `pidama` (`koosolek viidi läbi` →
`koosolekut peeti`); a survey, competition, tender or event is
`korraldama`; an analysis is `analüüsima`, a check `kontrollima`, an
audit `auditeerima`, a study `uurima`. Often the phrase just goes:
`tööde läbiviimise kvaliteet` → `tööde kvaliteet`. A route is not
kantseliit: `Rada viib läbi metsa`. When turning a -mine noun into a
verb, a particle verb is written apart (`läbivaatamine` → `läbi
vaatama`, `heakskiitmine` → `heaks kiitma`) and a noun+verb compound
has no compound verb (`riskihindamine` → `riske hindama`), while
`taaskasutamine` → `taaskasutama` stays joined.

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

**Don't pick the top fastText neighbour without inspecting**:
inflections and antonyms cluster high. Read the candidates.

**Don't change register silently.** If you're rewriting and the
register shifts, surface that explicitly: *"This rewrite is more
casual than the original. Want me to keep it formal?"*

**Don't use the surface form when you mean the lemma.** When the
user asks "what does this word mean," look up the lemma in
`synonyms`/dictionaries, not the inflected surface form.

**Don't editorialize about the MCP inside the deliverable.** Keep
tool mechanics out of the copy you hand the user. Lines like "the MCP
heuristic doesn't catch this, but..." belong in your working notes,
not in the polished output: they break the fourth wall and read as
hedging. If you spotted something a tool missed, just make the
correction cleanly. (And if you think a tool *should* have caught it,
check first: `check_redundancy` catches `samuti ka`-style doubling,
`check_compound_familiarity` tells you whether a compound is
attested, etc. The gap you assume may not exist.)

**Reference native-speaker intuition neutrally.** Prefer
*"emakeele kõneleja tajub seda kohe"* or *"eestlasele hakkab see kohe
kõrva"*. Avoid gendered framing like *"eesti mees kuuleb seda"*: it
reads folksy in client copy and native-speaker intuition isn't male.
(This is a minor polish, not a cardinal sin; the bigger anti-pattern
is the one above: don't narrate the MCP's limits inside the
deliverable at all.)

## Calling pattern reminders

- Tools are read-only and idempotent. Call them as often as
  reasoning demands; they're cheap.
- Input cap is 100,000 characters per text tool, 200 chars for
  `syllabify`. For very long texts, paragraph-batch them and merge
  the results.
- All Estonian-language results are returned in UTF-8. Preserve the
  characters õ, ä, ö, ü, š, ž literally in your output: don't
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
- "This sounds translated / make it sound native"
- "Make it shorter" / "Write this in Estonian" / "Translate this into Estonian"

When the user pastes Estonian text without an explicit instruction,
default to: spell-check first, surface anything questionable, then
ask what they want to do with it.
