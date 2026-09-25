# AIF: Agentic Identity Framework

**A portable, append-only memory of *you* that any AI model can read, knows when to consult, and
can't quietly corrupt.**

Every assistant is re-learning you from scratch: your stack, your diet, how you like answers, who
your sister is. Each vendor's memory feature is locked in its own database. AIF is one plain-text,
git-backed store you own. Claude, GPT, Gemini, local models, Claude Code, Cursor, or a bare chat box
all read and extend the same memory.

```
$ aif remember food diet vegetarian --since 2019
saved [k696bbcbbb] food · diet=vegetarian (U 0.95)

$ aif remember work employer Acme Corp
CONFLICT, not saved. `employer` holds one value and the file says: [k02ebc5963] work · employer=Nimbus Labs …

$ aif capsule            # ~750 tokens, forever, however much you store
```

## Why it's different

- **Models know when to look.** The context block includes a **map** of everything on file but not
  loaded (`travel 68: visited, airline_avoid, seat_pref`). The MCP `recall` tool's description
  carries the same map, live. A model about to book your flight can *see* that `seat_pref` exists,
  so it looks it up instead of guessing.
- **Flat context cost.** The capsule is filled best-first under a hard token budget. In a simulated
  decade (2,800 events, 690 current facts), it stayed at 745–769 of 900 tokens. At 5.2 tokens per fact,
  it costs 16× less than JSON. It is also byte-stable within a day, so prompt caches keep hitting.
- **Can't be poisoned by its own guesses.** Every fact records *who* said it: **U**ser, model
  **I**nferred, or **X** imported. A "user said" write needs the user's actual words. One model
  repeating a guess never raises its confidence. Several independent models agreeing do, but they
  are capped below "established". Inferences can't overwrite, retract or pin what you stated.
- **Survives everything.** Append-only JSONL in a git repo, with content-addressed events (edits are
  detectable even outside git) and union merge, so two devices never conflict. The store includes
  `AIF.md`, which explains the format to whatever model opens it. There are no dependencies: Python
  stdlib only, including the MCP server. [`SPEC.md`](SPEC.md) is short enough to re-implement.
- **Knows the people in your life, and that time passes.** Tell ChatGPT "my sister Rebecca is 11
  and at Lincoln Middle School". Two years later, ask Claude for a gift for "my sister", and it gets
  `Rebecca (your sister · age ≈13 (11 as of 2024-09) · aka Becca) … school=Lincoln Middle School
  (as of 2024-09, 2.0y ago)`, plus the instruction to ask only if that matters. Two sisters give one
  question ("Anna or Rebecca?"). An unknown "my brother" gives a question, not an invention.
- **Finds people you describe instead of name.** "The kid I coach" gives `LIKELY: Leo. Why: Leo is
  ≈11 (minor); you coach Hawks U12 soccer (shared with Leo)`, and says to confirm in passing if
  acting on it matters. Ambiguous descriptions give a shortlist and one question. An unknown name
  gives where it has come up.
- **A world-view, not just a fact list.** Models keep hypotheses about you ("comes alive when
  teaching", "over-commits in spring") that other models support, contradict and revise over
  months. Only your own words confirm or refute one, and they never pass as facts.
- **Private by default where it matters.** Health and finance are `private`, and they show up as counts
  in the map ("+3 private: ask the user first") rather than content. Pinned items like allergies are
  always shown.

## Quickstart

```bash
pip install git+https://github.com/<you>/agentic-identity-framework   # or: pip install -e .
```

```bash
aif init --owner "Your Name"
```

```bash
$EDITOR ~/.aif/core.md
```

`core.md` holds who you are in your own words, and should stay under ~300 tokens. Then add facts and notes:

```bash
aif remember health allergy shellfish --pin
```

```bash
aif remember style response_style "concise, code first, no filler"
```

```bash
aif note travel "Hates red-eyes; took one to Lisbon in 2024 and lost two days"
```

Connect your tools. This prints ready-to-paste config for Claude Code, Claude Desktop, Cursor, Codex, and more:

```bash
aif setup
```

For Claude Code, that's one line:

```bash
claude mcp add aif --scope user -- python3 -m aif serve
```

In a chat box with no tools, paste the capsule instead:

```bash
aif capsule | pbcopy
```

Your store is a git repo at `~/.aif`. Add a **private** remote and run `aif sync` on each device.

## People, pets, places, things

```bash
aif remember family sister Rebecca                       # introduces her: Rebecca is your sister
```

```bash
aif remember family school "Lincoln Middle School" --subject "my sister" --alias Becca
```

```bash
aif recall becca                                         # full dossier, with dates and history
```

```bash
aif people
```

```bash
aif recall "the kid I coach"                             # descriptive lookup across everyone's profile
```

```bash
aif beliefs                                              # what models currently think about you
```

Models do the same through `remember(subject=…, subject_relation=…)`. `recall("me")` returns your
own evolving profile.

## How a model sees it

On connect, the MCP server hands the client the **capsule** as `instructions`:

```
## Always true
health: allergy=shellfish

## People & things
Rebecca: sister, ≈13, aka Becca · Miso: dog · Priya: manager

## Active
family: Rebecca.likes=horses (as of 2024-09) · Rebecca.school=Lincoln Middle School (as of 2024-09)
food: diet=vegetarian · likes=thai curry, ramen · dislikes=cilantro?
style: response_style=concise, code first, no filler
work: employer=Helio Systems · role=founder

## Map (topic count: top predicates; recall for details)
travel 68 — visited, airline_avoid, seat_pref +36 notes
family 5 — sibling, pet, partner +33 notes
health 1 — allergy (+46 private: ask the user first)

## Check
If it comes up naturally, re-verify: employer=Helio Systems (last confirmed 2025-01) [k8b68bf158]
```

The server exposes three tools:

| tool | purpose |
|---|---|
| `recall(query?, topic?, history?)` | Compact lines with ids, or a person's dossier when the query mentions one ("my sister", "Becca", "me"). No args lists topics. A miss says *ask, don't guess*. |
| `remember(topic, predicate?, value, source, evidence?, subject?, subject_relation?, …)` | Saves a fact about you or someone/something in your life, or a freeform note if `predicate` is omitted. Returns `CONFLICT` / `POSSIBLE DUPLICATE` / `AMBIGUOUS` / `UNKNOWN` instead of writing when it should ask. |
| `reflect(propose\|support\|contradict\|revise, text?, id?, reason?, about?)` | Maintains the world-view: hypotheses about you or people in your life, weighed by evidence across models. |
| `amend(id, confirm\|retract\|pin\|unpin, evidence?)` | Changes status, including the user settling a belief. History is always kept. |

It also exposes resources `aif://capsule` and `aif://review`, and prompts `about_me` and `memory_review`.

## CLI

| command | what it does |
|---|---|
| `init` | create a store (a git repo with `AIF.md`, `core.md`, `events/`) |
| `remember T P V… [--subject --relation --alias --single/--multi --replace --pin --since --stale-after]` | state a fact |
| `note T TEXT…` | freeform wiki entry |
| `recall [Q…] [--topic --history]` | search |
| `forget / confirm / pin / unpin ID` | amend by id |
| `capsule [--budget N --stats]` | the injectable block |
| `show [TOPIC]` · `map` · `people` · `beliefs [--all]` | human wiki of everything · topic inventory · who/what you've mentioned · models' hypotheses about you |
| `review` | maintenance worksheet: renewals, unconfirmed inferences, conflicts, consolidation |
| `status` · `verify` | health · integrity (content hashes + git history has no deletions) |
| `export --format wiki\|capsule\|events` · `import FILE [--format lines\|aif]` | move in and out (e.g. another assistant's memory export) |
| `sync` · `serve` · `setup` | multi-device · MCP server · client config |

Set `AIF_HOME` to choose the store location, and `--clearance` / `AIF_CLEARANCE`
(`public|personal|private|secret`) to control what's revealed.

## Evidence

```bash
python3 -m unittest discover -s tests -t .   # 56 tests
```

```bash
python3 evals/run.py                         # deterministic, writes evals/RESULTS.md
```

```bash
python3 evals/when_to_call.py                # live model eval, needs Anthropic credentials, costs money
```

[`evals/RESULTS.md`](evals/RESULTS.md) covers format cost, 10-year growth with a burst import,
the hallucination feedback loop, 8/8 conflict scenarios, 11/11 people-over-time checks, and 9/9 descriptive-lookup and world-view checks.

## Docs

- [`SPEC.md`](SPEC.md): the `aif/1` format (normative), for other implementations
- [`docs/DESIGN.md`](docs/DESIGN.md): rationale, what changed from the original draft, prior art, and
  honest limitations

MIT licensed.
