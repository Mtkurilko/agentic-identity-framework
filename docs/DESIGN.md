# Design notes

AIF began as the *Personal Identity Graph v1* draft: tiers, append-only log, conf/salience split,
provenance, budgeted top-K, git as substrate. The draft had the right instincts. This document
records where building it changed the design, and why.

## The three problems

1. **Models don't know when to look.** A memory tool that is never called is worthless, and models
   rarely call a tool whose value they can't see. Most memory systems either inject everything, which
   costs tokens, or hide everything behind a search tool, which then goes unused.
2. **Context is expensive and has to stay flat.** Personal memory grows forever. The prompt must not.
3. **Vendors, formats and models come and go.** Memory kept in a provider's database dies with the
   provider. The memory has to outlive any tool that reads it.

## 1. Knowing when to call: progressive disclosure for personal memory

The draft had no mechanism for this. AIF uses a *map*, which shows the **shape** of what is on file
without its **content**:

```
## Map (topic count: top predicates; recall for details)
travel 68 — visited, airline_avoid, seat_pref +36 notes
work 50 — project, tool, employer, role +37 notes
health 1 — allergy (+46 private: ask the user first)
```

A dozen lines cost roughly 150 tokens. From them, a model that is about to book a flight sees
that `seat_pref` and `airline_avoid` exist and calls `recall`. This mirrors how agent skills work:
the name and description are always visible, and the body loads on demand. The map is delivered in three
places, so it reaches models in every setting:

| channel | reaches | always in context? |
|---|---|---|
| capsule `## Map` (via MCP `instructions`, SessionStart hook, or paste) | every model, tools or not | yes |
| `recall` tool **description** is regenerated with a one-line map; `tools/list_changed` fires when a new topic appears | MCP clients | yes, since tool descriptions persist all session |
| `recall` result "Nothing on file. Ask the user rather than guessing" | MCP clients | on miss |
| `aif hooks` (UserPromptSubmit): names a person on file when the message mentions them | Claude Code | per message |
| `aif hooks` (UserPromptSubmit + Stop): flags self-disclosures, and asks once at turn end if nothing was saved since | Claude Code | per turn |

The map also solves search. Because the model sees the store's own predicate vocabulary
(`seat_pref`, not "where I like to sit"), plain BM25 retrieves well. Embeddings become optional, not a
dependency. Semantic de-duplication, which the draft assigned to embeddings, is handed to the
calling model: `remember` returns `POSSIBLE DUPLICATE` with the candidate, and the model, which
understands language, decides.

Saving has the opposite problem: a model rarely *forgets* to answer, but it often forgets to write
down what it just learned. Descriptions alone don't fix that, so the hooks add a deterministic check.
The prompt hook records sentences that look like durable self-disclosure, along with the last event
id in the store at that moment. The stop hook compares: if nothing has been written since, it asks the
model once to save (or to do nothing). Pattern matching is crude, so it is tuned to miss rather than
nag. A false positive costs one look, and the same sentence never triggers twice.

`evals/when_to_call.py` measures this directly: recall-call rate on questions whose answer is on
file but not loaded, against a baseline memory tool with a generic description. **It has not been run
yet** (it needs API credentials). Treat the claim as a hypothesis until it has numbers.

## 2. Context efficiency

| decision | why |
|---|---|
| **Hard token budget**, best-first fill (draft §4.2, kept) | Measured: after a 150-fact import, a `score > 0.4` threshold injects 2,044 tokens and the budget injects 756 (`evals/RESULTS.md`). |
| **Grouped micro-format** `food: diet=vegetarian · likes=thai, ramen` | 5.2 tokens/fact, against 24.6 for the draft's pipe table and 83.6 for JSON. Most savings come from dropping per-row metadata (conf/sal/src/dates) *from the injected view*, because a model only needs "is this tentative?", which `?` conveys. Full metadata is one `recall` away. |
| **Cache-stable rendering** | Ordered by topic/predicate rather than score, with time evaluated per day. The capsule is byte-identical across a day's sessions and only changes when membership changes. Provider prompt caches stay warm, and git diffs stay one line long. The draft's "lazy re-retrieval on topic drift" is unnecessary: the capsule is static per session and recall is on demand. |
| **"Patch, don't regenerate"** | Done in memory: new events are applied to the cached graph instead of replaying the log. The committed `capsule.md` is fully re-rendered, but deterministically, so the *diff* is the patch. A full replay of ten simulated years (2,800 events) takes ~25 ms, so no SQLite index is needed. |
| **Floor under the budget** (score ≥ 0.05) | A budget alone would fill spare room with near-dead facts. The floor keeps unused budget unused. |

## 3. Surviving everything

| decision | why |
|---|---|
| **Content-addressed events** (id = hash of body) | Tamper evidence that survives *outside* git (a copied folder, a zip, a sync service). Identical events from two devices collapse to one. |
| **Git with `merge=union` on the log** | The draft proposed git but missed that two devices appending to the same JSONL is a textbook merge conflict. Union merge is built into git: both sides' lines are kept, replay sorts by time, and duplicates dedupe by id. `aif sync` = pull, re-render, push. |
| **Month-sharded log** | Files stay small and diffs stay local. Nothing depends on the sharding. |
| **Commit per write, with a greppable message** | `git log --oneline` reads as a timeline of who asserted what: `[by:claude-opus-5-5] [I 0.50] style/tone=dry humour`. `aif verify` also scans git history for any commit that *removed* a log line. |
| **`AIF.md` in every store** | An agent pointed at the repo with no tooling learns the rules from the repo itself. Its schema history is written as instructions to future readers, so the changelog doubles as the migration guide (draft §10.3, kept). |
| **Zero dependencies, Python ≥ 3.9 stdlib** | This includes the MCP server, which is ~200 lines of JSON-RPC over stdio. Fewer dependencies means fewer ways to rot. `SPEC.md` is short enough to re-implement in an afternoon. |
| **Exports**: wiki markdown, capsule, raw events | The wiki is the human-readable "encyclopedia of you". Raw events are the lossless interchange. Imports are marked `X` and land as notes, and structuring them is left to a model rather than guessed at import time. |

## 4. Trust: the draft's best idea, made enforceable

The draft's U/I/X provenance was its most important insight: without it, a model reads its own
earlier guess as fact and compounds it. AIF turns the idea into rules that code enforces, not
conventions:

- **`U` requires evidence.** A model can't claim "the user said so" without quoting the user.
  Without a quote, the write is silently downgraded to `I` and the model is told why.
- **Witnesses are keyed by contributor.** In the simulation, one model re-asserting a wrong guess
  eight times leaves confidence at 0.50. The naive "+weight per mention" rule reaches 1.00.
- **Independent models count, up to a point.** The draft's multi-witness consensus is kept as noisy-OR
  over *distinct* contributors, capped at 0.79 so it can never cross "established" (0.8). Only
  `confirm` with the user's words, or a `U` assert, establishes.
- **Inferences can't overwrite, retract or pin what the user stated.** These are refusals, not warnings.
- **Cardinality-aware conflicts.** The draft flagged a conflict on any same-subject-and-predicate
  pair, which would fire on `likes: Beatles` vs `likes: Radiohead`. AIF only flags single-valued
  slots. The tool returns `CONFLICT, not saved` with a one-line question for the user, and the
  answer is written with `replace=true`. A stated fact may silently replace a merely *tentative*
  inference, since asking would be noise.
- **Sensitivity and clearance** (absent from the draft). A file you paste into arbitrary models needs
  this. Health and finance default to private. Private items appear in the map only as counts
  ("+46 private: ask the user first"), so a model knows to ask without learning the content.
  Pinned items (allergies, hard constraints) are always shown, because the user chose that.

## 5. People, places and things: memory that knows *who*, and *when*

A human remembers "my sister Rebecca, the horse-mad one in middle school" as one identity, not
three loose facts. They also know that was two years ago. AIF models the same thing, with more
precision than a person manages:

- **Entities.** Anyone or anything the user talks about becomes a subject with its own facts, a
  name, a kind, relations to the user and nicknames. "My sister is Rebecca" arrives as
  `me.sister=Rebecca` and is recognised as an *introduction* rather than a fact about the user.
- **Resolution on read and write.** "My sister", "my little sis", "Becca" and `[rebecca]` all resolve
  to the same person in `recall` and in `remember(subject=…)`. The capsule carries a one-line roster
  (`Rebecca: sister, ≈13, aka Becca · Miso: dog`), so a model hears "my sister" and already knows
  who that is and that more is on file.
- **Ambiguity is a question, never a guess.** Two sisters → `AMBIGUOUS … ("Anna (sister) or Rebecca
  (sister)?")`. An unknown "my brother" → `No brother on file … ask once for their name`. A coworker
  also called Rebecca becomes `rebecca_coworker`, kept separate.
- **Time-awareness.** Every fact knows when it was last confirmed. Time-sensitive details (school,
  grade, job, a child's interests) get a shelf life, and once past it they render as
  `school=Lincoln Middle School (as of 2024-09, 2.0y ago)`. Ages are computed, never stored stale:
  "11 in Sep 2024" is shown as `≈13`. The code does the arithmetic. Whether "13 and two years on
  probably means high school" matters for *this* request is left to the model.
- **Don't spam.** The capsule and every dossier carry the same rule: ask at most one short question,
  and only when the answer changes what you'd do. A stale school doesn't matter for "what does she
  like to draw?", but it does for "help her with her homework".
- **Change is history, not overwrite.** When the user says Rebecca started high school, the new value
  supersedes the old one and the dossier shows `earlier: school=Lincoln Middle School
  (2024-09→2026-09)`. `recall("me")` gives the same evolving profile of the user.

**Finding someone without a name.** People don't say names. They say "the kid I coach", "the vet
we use", "my friend from climbing". The capsule stays small, but `recall` goes deep. It builds a
profile document for every entity from everything connected to it, and searches those:

- its own facts, facts that point *at* it, and notes that name it;
- **one hop through shared values**: `me.coaches=Hawks U12` and `leo.plays_for=Hawks U12` are two
  unrelated facts, but together they make Leo "a kid I coach";
- a projected **age band**: "kid" matches anyone computed to be under 18, while "my kid" means the
  relationship;
- **IDF weighting**: "striker" (one person) outweighs "kid" (several). Words nothing on file
  contains count half against everyone, so "the kid I coach who loves dinosaurs" drops from LIKELY
  to POSSIBLE.

The answer is graded, with its reasons. **LIKELY** gives a name, a "why", and "confirm in passing if
acting on it matters". **POSSIBLE** gives a shortlist and one question. **Nothing** gives "ask who
they mean". A proper name with no profile ("Marco") returns the notes that mention him and who
he's mentioned alongside, rather than a guess at someone else. Writes about a description never
land on a guess: `UNSURE, not saved … probably Leo`. After the user confirms, the model saves the
description as an alias, so the next lookup is direct.

**Relationships have history.** "My partner is Jordan" is recorded as `me.partner=Jordan`, an
ordinary single-valued fact. When it replaces `me.partner=Sam`, Sam's file reads "formerly: your
partner (2026-03→2027-05)". Nothing is overwritten, and nothing is forgotten.

No new event type was needed for entities. They are facts with four reserved predicates, so any `aif/1` reader
already stores them correctly (see SPEC §3.4–3.5). `evals/RESULTS.md` §5 walks the full scenario
(ChatGPT learns about Rebecca in 2024, Claude is asked in 2026): 11/11 checks pass.

## 6. The world-view: beliefs that are cultivated, not asserted

Facts are what the user said. Understanding someone is more than that: noticing that they come
alive when teaching, that they over-commit every spring, that "fine" from them means "not fine".
Most memory systems either drop this or, worse, store it as fact. AIF gives it its own layer:

- `reflect(propose)` records a **hypothesis** ("Alex's identity is bound up in mentoring kids"),
  starting tentative (≤ 0.6).
- Later conversations, and *other models*, `support` or `contradict` it with what they observed.
  Confidence moves on log-odds. A new model's independent observation counts fully, and the same
  model repeating itself counts less and less. Contradictions weigh more than support.
- `revise` replaces a reading with a better one and keeps the lineage ("revised from: …").
- Models alone cap at 0.85. **Only the user's own words confirm or refute.** A refuted belief stays
  in history ("we used to think X; the user said no") but leaves the capsule.
- The capsule carries a small **Understanding** section (at most 6 items and 15% of the budget),
  labelled as hypotheses: "use it to anticipate, never to overrule the user".

`evals/RESULTS.md` §6 traces one belief over fourteen months: proposed by Claude, supported by GPT
and Claude, contradicted by Gemini, revised, then confirmed by the user (0.40 → 0.52 → 0.64 →
0.45 → 0.95). This is the difference between a memory that stores what you said and one that is
slowly getting to know you. It stays inspectable and correctable (`aif beliefs`, `aif forget b…`).

## 7. Maintenance: the model does the semantics, the code does the bookkeeping

`aif review` (also the `memory_review` MCP prompt) prints a worksheet any model can work through with
the user in one message:
- **Renewal**: established facts whose salience has faded ("still at Nimbus Labs?"). This is the
  draft's passport-renewal idea.
- **Unconfirmed inferences**, highest confidence first.
- **Contested slots**, for example after two devices synced conflicting values.
- **Possibly outdated** time-sensitive details, oldest first. Shelf lives are learned from the
  user's own history once a detail has changed twice. Otherwise they come from defaults per detail
  type, or from a `stale_after` the model sets when saving.
- **Beliefs worth checking**: well-observed or disputed hypotheses, to settle with the user.
- **Consolidation clusters**: topics with 5+ unconsolidated notes. The model summarises them into
  facts with `derived_from=[note ids]`. The notes stay in the archive but leave the map.

Between reviews, the capsule's `## Check` line carries one faded fact that the model re-verifies "if
it comes up naturally". These are the draft's natural spot-checks, at a cost of about 20 tokens.

## What is and isn't new

Honest positioning. **Prior art**: Graphiti/Zep (bi-temporal validity windows, which AIF adopts as
`since/until` plus record time), Mem0 (extract-and-retrieve), Generative Agents (recency ×
importance × relevance scoring), MemGPT/Letta (tiered memory), and the Claude/ChatGPT memory
features. **What AIF adds, as a combination we haven't seen shipped elsewhere:**

1. A **map of what exists but isn't loaded**, placed in the always-visible tool description and
   kept live with `list_changed`, as the answer to "when should the model look?".
2. **Provenance that code enforces**: evidence-required user statements, contributor-keyed witnesses,
   a ceiling on inferred confidence, and refusals for inference overwriting statement.
3. A **cache-stable, budget-bounded capsule** whose correctness doesn't depend on tools, since it is
   self-describing when pasted raw.
4. **A git repo as the database**, made multi-device-safe with content-addressed events and union merge.
5. **Zero infrastructure**: a single stdlib package, a two-page spec, and plain files you own.
6. **Entities with time**: people and things resolved from names, nicknames, relations and
   descriptions ("the kid I coach", via shared values and age bands), a
   question-not-guess policy on ambiguity, computed ages, and per-detail shelf lives that surface
   "as of" dates.
7. **A world-view layer**: hypotheses that models propose, support, contradict and revise across
   vendors and months, confirmed or refuted only by the user, kept strictly separate from facts.

## Known limitations / roadmap

- `when_to_call` has not been run yet, so the core claim above has no measured numbers. Running it is the
  next step.
- There is no encryption at rest. `secret` is only a rendering rule. Plan: `age`-encrypted
  `events/secret/*.jsonl` shards.
- Lexical search misses pure synonyms that the map's vocabulary doesn't cover. An optional embedding
  hook is on the roadmap, deliberately not a dependency.
- Descriptive lookup is lexical plus graph structure (shared values, age bands, IDF). It finds "the
  kid I coach" when the facts connect, but not pure paraphrase with no shared words ("the young
  one on my team" with nothing saying "team"). An optional embedding scorer over the same profile
  documents is the natural next step, and the profile builder was written to make it drop-in.
- Belief updates are a simple log-odds heuristic, not calibrated probabilities. The numbers are
  for ordering and thresholds, not claims about the world.
- The stdio MCP server reaches desktop and CLI clients (Claude Desktop, Claude Code, Cursor, Codex).
  Web apps such as ChatGPT or claude.ai need a remote (HTTP) MCP endpoint with authentication, which
  isn't built yet. Until then, reach them with `aif capsule`/`aif import`.
- Clients differ in how they surface MCP `instructions`. The SessionStart hook and paste paths cover
  the gaps.
- The token counter is a heuristic (±15%) unless `tiktoken` is installed. Budgets are meant to have
  headroom.
- Snapshots / "who I was in 2026" diffs (draft §5.10) come for free from git (`git checkout <date>`
  followed by `aif capsule`), but have no dedicated command yet.
