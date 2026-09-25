"""Files written into a new store. AIF.md is addressed to whatever model opens the repo."""

AIF_MD = """\
# AIF memory store (format aif/1)

This repository is one person's portable long-term memory. It is written for AI models
as much as for humans: if you are a model reading this, these are your instructions.

## Files
- `core.md`: who the owner is, in their own words. Hand-written. Read it; never edit it
  unless the owner asks.
- `events/YYYY-MM.jsonl`: THE ground truth. One JSON event per line, append-only.
- `capsule.md`: a compact, budgeted summary rendered from the events. Paste it into
  any model. Regenerated on every write; never edit it by hand.
- `aif.json`: settings (owner name, token budgets).

## Reading
Facts are (subject, predicate, object) triples with provenance:
- `src`: U = the user said it, I = a model inferred it, X = imported from another system.
- A fact inferred by models (I) is tentative. Confirm it with the user before relying on it.
  Agreement from several *different* models raises confidence, capped at 0.79. Only the
  user's own words make a fact established (>= 0.8).
- A fact is current until a later `retract` event, or an `assert` whose `supersedes` names
  it, closes it. History is never deleted.
- People, pets, places and things are subjects other than `me`. Who they are is stored as facts
  with the predicates `name`, `kind`, `relation_to_me` and `alias` (e.g. s=rebecca p=relation_to_me
  o=sister). Resolve "my sister" or a nickname to them before assuming anything.
- `belief` events are models' working understanding of the user (or someone in their life):
  interpretations, not facts. `evidence` events support (+) or contradict (-) them, and
  `revises` links a better reading to the one it replaces. Only the user's own words (`ev`)
  confirm or refute a belief. Never present a belief to the user as fact.
- Time matters: `t` is when something was recorded. A time-sensitive detail (school, grade, job,
  a child's interests) recorded long ago may no longer be true. Ages move forward from the date
  they were recorded. Ask about an old detail only when it matters to the task at hand.

## Writing (tools: `aif remember` / `aif note` / `aif forget`, or the MCP tools)
1. Never modify or delete an existing line. To change something, append an event.
2. `src: "U"` requires `ev`: the user's verbatim words. Without them, write `src: "I"`.
3. Never pin (`pin`) anything the user did not ask to pin.
4. For single-valued predicates (employer, lives_in, diet, ...), a different value is a
   CONFLICT. Ask the user which is current before superseding.
5. Reuse existing topics and predicate names (see the map in capsule.md) before inventing new ones.
6. Facts about someone else use their id as `s`. Introduce a new person once with `name` and
   `relation_to_me` facts. If a mention could be two people, ask rather than pick.

## Event shape (one line each; `id` = first 16 hex of sha256 of the canonical body)
{"v":1,"t":"2026-09-25T16:40:00Z","op":"assert","s":"me","p":"diet","o":"vegetarian",
 "topic":"food","src":"U","by":"claude-opus-5-5","conf":0.95,"ev":"I've been vegetarian since 2019",
 "since":"2019","imp":2,"id":"…"}
ops: assert, retract{ref,reason}, confirm{ref,src,by,ev}, pin{ref,src}, unpin{ref},
     note{text,topic,title?,about?}, define{p,card:one|many,topic,desc},
     belief{about,text,kind?,conf,revises?,basis?}, evidence{ref,dir:+|-,reason?,ev?}
optional on assert: stale_after (days until a time-sensitive detail should be re-checked)

## Schema history (the changelog is the migration guide)
- v1 (2026-09): initial format. A future reader that meets v1 events alongside newer ones
  should interpret v1 exactly as described above and must not rewrite them. Migrations are
  new events, never edits.
- v1, entities (2026-09): additive conventions, no new ops. Reserved predicates `name`, `kind`,
  `relation_to_me`, `alias` on non-`me` subjects; optional `stale_after` on assert and `about`
  on note. A reader that doesn't know these still stores and replays them correctly.
- v1, world-view (2026-09): new ops `belief` and `evidence`. Readers that predate them ignore
  them (unknown ops are skipped), so older tools still read every fact correctly.
"""

CORE_MD = """\
<!-- Who you are, in your own words. Injected into every session: keep it under ~300 tokens.
     Good things to put here: how you like to be spoken to, what you're working toward,
     hard constraints. Put individual facts in the event log instead (`aif remember`). -->
"""

GITATTRIBUTES = """\
# Append-only logs merge by union: two devices adding facts never conflict.
events/*.jsonl merge=union
# The capsule is derived; `aif sync` re-renders it after any merge.
capsule.md merge=union
"""

GITIGNORE = """\
.lock
.cache/
"""
