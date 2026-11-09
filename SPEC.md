# AIF format specification: `aif/1`

Status: draft 1 (2026-09). This document is normative. Another implementation that follows it can
read and write the same stores as the reference implementation in `aif/`.

The key words MUST, SHOULD and MAY are used as in RFC 2119.

## 1. Store layout

A store is a directory, and SHOULD be a git repository.

| path | role | written by |
|---|---|---|
| `events/YYYY-MM.jsonl` | **ground truth**, append-only | tools only |
| `core.md` | owner's self-description, free text (HTML comments are ignored) | the owner |
| `capsule.md` | derived rendering (§6), committed for tool-less access | tools only |
| `aif.json` | settings: `format`, `owner`, `budget`, `core_budget` | owner/tools |
| `AIF.md` | human/model-readable description of this format and its rules | `init` |
| `.gitattributes` | MUST contain `events/*.jsonl merge=union` | `init` |

Everything except `events/` and `core.md` can be regenerated. An implementation MUST NOT require any
other file to exist.

## 2. Events

One JSON object per line, UTF-8. Events are sharded by the month of `t` and are never modified or
removed once written.

### 2.1 Common fields

| field | type | meaning |
|---|---|---|
| `v` | int | schema version (1) |
| `t` | string | record time, UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ` |
| `op` | string | one of §2.2; unknown ops MUST be ignored (and MAY be reported) |
| `id` | string | first 16 hex chars of SHA-256 over the canonical body (§2.3) |
| `by` | string | contributor: a model id (`claude-opus-5-5`), client name, or `user-cli` |
| `src` | `U`\|`I`\|`X` | provenance: **U**ser stated, model **I**nferred, **X** imported |

Readers MUST preserve unknown fields when re-exporting and MUST otherwise ignore them.

### 2.2 Operations

| op | required | optional | effect |
|---|---|---|---|
| `assert` | `s p o topic src by conf` | `ev imp since until sens pin card supersedes from` | claim a fact (§3) |
| `confirm` | `ref src by` | `conf ev` | add a witness to an existing fact |
| `retract` | `ref` | `reason ev by` | close a fact or note (history is kept) |
| `pin` / `unpin` | `ref` (+`src` for pin) | `ev by` | pin only takes effect when `src` = `U` |
| `note` | `text topic src by` | `title sens about` | freeform knowledge (wiki entry, episode); `about` = entity id |
| `define` | `p` | `card topic desc` | declare a predicate; `card` is `one` or `many` |
| `belief` | `about text src by` | `conf kind basis revises reason ev sens` | a hypothesis about `me` or an entity (§3.6) |
| `evidence` | `ref dir src by` | `reason basis ev` | `dir` `+` supports, `-` contradicts a belief |
| `merge` | `from into src by` | `reason ev` | two entity ids are the same person or thing (§3.4) |

Field meanings:
- `s` subject (`me` = the owner; otherwise a snake_case name), `p` predicate (snake_case),
  `o` object (free text), `topic` (snake_case).
- `conf` 0–1, the writer's confidence. `ev` is the owner's verbatim words. `imp` is importance 1–3 (default 2).
- `since` / `until` are **world time** (when it was true, any ISO prefix: `2019`, `2024-03`). `t` is
  **record time**. Keeping the two separate makes the log bi-temporal.
- `sens` is one of `public` < `personal` (default) < `private` < `secret`.
- `supersedes` is a list of fact keys this assert closes. `from` is a list of note ids this fact summarises.
- `stale_after` (int, days) overrides how long a time-sensitive detail stays fresh (§3.5).

### 2.3 Identity

- **Event id**: `sha256(canonical)[:16]`, where `canonical` is the event without `id`, serialised
  with sorted keys, separators `,` `:`, and no ASCII escaping. A line whose `id` does not match its
  content was edited, and readers MUST report it. A line with no `id` was written by hand; readers MUST
  accept it and compute its id.
- **Fact key**: `"k" + sha256(norm(s) "|" slug(p) "|" norm(o))[:9]`. `norm` lowercases, collapses
  whitespace and strips surrounding `.,!;:`. `slug` maps non-`[a-z0-9]` runs to `_`.
- **Note id**: `"n" + event_id[:9]`. **Belief id**: `"b" + event_id[:9]`.

## 3. Replay

State is derived by applying events in a **stable sort on `t`**, keeping file order for ties, with
duplicate `id`s skipped. A fact is **open** until a `retract` or a superseding `assert` closes it, or
until it carries an `until`. Re-asserting a closed fact re-opens it.

### 3.1 Confidence (provenance-gated)

Witnesses are keyed by `(src, by)`. Each keeps its max `conf`.

```
if any U witness:            conf = max(U confs)
else:
  inferred = min(1 - Π(1 - min(c_i, 0.6)) over distinct I contributors, 0.79)
  conf     = max(max(X confs), inferred)
established := conf >= 0.8        tentative := not established
```

As a result:
1. A model repeating itself adds nothing, because the same contributor is one witness.
2. Independent models agreeing is evidence, via noisy-OR.
3. Inference alone can never establish a fact. Only the owner's words can.

### 3.2 Salience

```
salience = min(1, IMPORTANCE[imp] · 0.5^(age_days / HALF_LIFE[src]) · min(1.5, 1 + 0.15·ln(mentions)))
IMPORTANCE = {1: .35, 2: .6, 3: .9}      HALF_LIFE = {U: 365, X: 270, I: 90}
age = now - last assert/confirm          pinned facts: salience = 1
score = conf · salience
```

### 3.3 Cardinality

A predicate's cardinality is set by the latest `define`, or the latest `assert` carrying `card`.
Otherwise it defaults to `one` for the built-in list (`name, lives_in, employer, role, diet, timezone,
partner, …`) and to `many` for everything else. Two open facts in the same `one` slot form a
**contested slot**. It is reported, never silently resolved.

### 3.4 Entities

Any subject other than `me` is an **entity**: a person, pet, place, org, thing, project or event.
Its identity is carried by ordinary facts with four reserved predicates. They are reserved only
on subjects other than `me`, since `me.name` is an ordinary fact.

| predicate | card | example |
|---|---|---|
| `name` | one | `s=rebecca p=name o=Rebecca` |
| `kind` | one | `person`, `pet`, `place`, `org`, `thing`, `project`, `event` |
| `relation_to_me` | many | `sister`, `manager`, `dog` (as the user said it) |
| `alias` | many | `Becca` |

A relationship to the user SHOULD be recorded as an ordinary fact about `me` whose value is the
entity's name (`me.partner=Jordan`), so it has history: when it is superseded, the entity becomes
*formerly* the user's partner. `relation_to_me` is for relationships stated from the entity's side.
Entity ids are `slug(name)`. When a second, different entity has the same name, its id gets a suffix,
usually the relation (`rebecca_coworker`). An open `me.<p>=<X>` fact where `X` is an entity's name
also adds `p` as a relation. Resolution of a mention, strongest first:
exact id → name → alias → first name → relation word ("my sister", "sis") → wider relation
("my sibling" finds a sister, never the reverse). When the top two candidates are within 0.1 of each
other, the mention is **ambiguous** and a writer MUST ask instead of choosing.

**Merging.** When the owner says two entities are one (`thomas` and `thomas_bruce`), a `merge`
event folds `from` into `into`. On replay, every fact about `from` moves to `into` and keeps its
key, so ids handed out before the merge still resolve. `from`'s `name` becomes an `alias`. A moved
fact that `into` already holds (same predicate and value, or a second `kind`) is closed with reason
`merged into <id>`, so nothing counts twice. Notes and beliefs about `from` move too. Later events
that still name `from` as `s` or `about` are redirected, following chains of merges. Merging into or
out of `me`, or an entity into itself, is reported as a problem and ignored.

**Descriptive resolution** ("the kid I coach"), for when nothing above matches: each entity gets a
profile document. It is built from its name and aliases, relations, kind, projected age band
(`minor`/`adult`/`senior`), its facts, facts that point at it, **one hop through shared values**
(`me.coaches=Hawks U12` and `leo.plays_for=Hawks U12` connect Leo to the user's coaching), notes
about or naming it, and beliefs about it. Query words map to profile vocabulary ("kid" → `minor`,
except after "my/our", where it is the relation). Candidates are ranked by IDF-weighted coverage.
Words no profile contains count half against every candidate, and an unknown age earns half credit
for an age word. The result is `likely` (coverage ≥ 0.8 with a clear margin), `possible` (≥ 0.5)
or `none`. A reader MUST present `likely` as an inference, never as fact, and a writer MUST NOT
write to a descriptively resolved subject without the owner's confirmation.

### 3.5 Time

- **Staleness**: a detail is *possibly outdated* once `now − last_t` (last assert/confirm) exceeds its
  shelf life: the fact's `stale_after`, else the predicate default (e.g. `school` 365, `grade` 300,
  `employer` 730, `lives_in` 1095, `current_*` 120), else — for a person under 18 — 365 days for
  `likes/hobby/interest`. Confirming or re-asserting resets it. Renderers mark it `(as of YYYY-MM)`.
- **Learned shelf life** takes precedence over the defaults. It applies once a predicate has two or
  more ended values in the log: 75% of the median observed lifetime (minimum 60 days). A
  household that changes schools every four years isn't nagged every year.
- **Age** is never stale. It is computed: from `birthday`/`born` when known, otherwise from
  `age` + whole calendar years since it was recorded (`since`, else `t`).

### 3.6 Beliefs (the world-view)

A belief's confidence starts at its `conf` (≤ 0.6) as log-odds. Each supporting `evidence` adds
`0.5 / k`, where `k` is how many times that contributor has already supported it, so a new
model's independent view counts fully. Each contradiction subtracts 0.8. Without the owner the
result is capped at **0.85**. An `evidence` or `belief` with `src: U` (the owner's words in `ev`)
settles it: `confirmed` (0.95) or `refuted`. A `belief` with `revises` closes the old one as
`revised`, and the lineage is kept. Beliefs never become facts and MUST NOT override facts or the
owner's statements. Renderers include open beliefs with confidence ≥ 0.3 in an **Understanding**
section, capped at a small share of the capsule budget (reference: 6 items, 15%).

## 4. Write rules (MUST for any writer)

1. Append only. Never rewrite or delete a line.
2. `src: U` requires `ev`. Without the owner's words, write `I`.
3. Only `src: U` may pin.
4. An `I` writer MUST NOT supersede or retract a `U` fact.
5. Before asserting a new value into an occupied `one` slot, the writer MUST ask the owner, unless every
   occupant is a tentative inference. Then it sets `supersedes`.
6. Writers SHOULD reuse existing topics and predicates.
7. Facts about someone else MUST use their entity id as `s`. An ambiguous mention MUST be resolved
   with the owner, not guessed.
8. A `merge` MUST be `src: U` with the owner's words in `ev`. A model that only suspects two entries
   are the same asks.

## 5. Sensitivity

A reader has a **clearance**. Items above it are withheld, except pinned items, which are shown up to
`private`. `secret` items are never rendered for a model. Topics `health, medical, finance, money,
legal` default to `private`. The *existence* of withheld items MAY be signalled as counts, so a model
knows to ask.

## 6. Capsule (recommended rendering)

The capsule is what gets injected. A conforming renderer SHOULD:

- start with a short self-describing header: what the text is, that `?` = tentative, and that the map
  lists more than is loaded;
- include `core.md`, then pinned facts, then a one-line roster of **People & things**, then a few
  **Understanding** beliefs, then **Active** facts selected **best-first by score under a
  hard token budget** (never a score threshold), then a **Map** of all topics with counts and top
  predicates, then at most one faded fact to re-verify;
- render facts as `topic: pred=value, value2 · pred2=value?`, grouped by topic;
- order output by topic/predicate rather than by score, and evaluate time at day granularity, so the
  text is byte-stable until membership changes. This keeps prompt caches warm.

## 7. Versioning

`AIF.md` carries a plain-language schema history addressed to future readers. A new version MUST
remain able to read `v: 1` events as specified here. Migrations are expressed as new events, never
as rewrites.
