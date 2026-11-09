# AIF

A personal memory that you own, and that every AI you use can read and add to.

Each AI app keeps its own memory of you, locked inside that app. Switch from ChatGPT to Claude, or
from Cursor to Claude Code, and you start over. AIF keeps that memory in a folder on your computer
instead: plain text files in a git repo. Any assistant that supports MCP can read it and write to
it, and you can paste it into any chat that doesn't.

It holds three things:

- **Facts about you and the people in your life.** "Vegetarian since 2019." "My sister Rebecca is 11."
- **How you want your AI to behave.** Tone, how blunt to be, when to push back, what never to do.
  This goes with you to every model.
- **What models have figured out about you.** Kept apart from facts, marked as guesses, and
  never treated as true until you say so.

No accounts and no cloud service. It runs on the Python standard library alone.

## Install

Requires Python 3.9+ and git.

```bash
pipx install git+https://github.com/Mtkurilko/agentic-identity-framework
```

`pip install` works too.

## Set up

**1. Create your memory.** The first run asks five short questions. Skip any you like.

```bash
aif init
```

**2. Connect your AI tools.** For Claude Code:

```bash
claude mcp add aif --scope user -- aif serve
```

```bash
aif hooks install
```

The hooks are optional, but they do most of the work of making memory stick. See
[Why it doesn't forget](#why-it-doesnt-forget).

For Claude Desktop, Cursor, Windsurf, Codex and other MCP clients, run `aif setup`. It prints the
config to paste into each one.

**3. Use your AI as usual.** When you mention something worth keeping, the model saves it.
When it needs to know something about you, it looks it up.

For a chat app without MCP support, copy the summary and paste it in as your first message or
your custom instructions:

```bash
aif capsule | pbcopy
```

## Your AI, your way

`persona.md` describes how you want any assistant to act with you. Write it once and it applies
to every model, from any company.

```bash
aif edit persona
```

For example:

```
Be direct. If my plan has a hole, say so before helping with it.
Short answers by default; I'll ask for more.
Never use exclamation marks or tell me something is a great question.
```

Models can add to it too. If you tell one "stop apologizing so much", it saves that under the
`persona` topic, and every other model sees it next time. `core.md` (`aif edit`) is the same kind
of file, but it describes you rather than your assistant.

To fill everything in at once, run the `get_to_know_me` prompt from any MCP client. The model
interviews you for a few minutes and saves the answers as it goes.

## What a model sees

When a model connects, it gets a short summary called the capsule. It stays under about 900
tokens however much you store:

```
## Persona (how the user wants their AI to be; follow it)
Be direct. If my plan has a hole, say so before helping with it.
pushback=on weak plans · tone=dry humor

## Always true
health: allergy=shellfish

## People & things
Rebecca: sister, ≈13, aka Becca · Miso: dog · Priya: manager

## Active
food: diet=vegetarian · likes=thai curry, ramen · dislikes=cilantro?
work: employer=Helio Systems · role=founder

## Map (topic count: top predicates; recall for details)
travel 68 — visited, airline_avoid, seat_pref +36 notes
health 1 — allergy (+46 private: ask the user first)
```

The **map** lists what is on file without loading it. A model booking a flight can see that
`seat_pref` exists, so it calls `recall` instead of guessing. A `?` marks a model's guess the user
hasn't confirmed. Health and finance details are private by default and appear only as counts.

The model gets four tools:

| tool | what it does |
|---|---|
| `recall` | Look something up by keyword, topic, name ("Becca"), relation ("my sister") or description ("the kid I coach"). |
| `remember` | Save a fact or note about you or someone in your life. When it should ask first, it returns `CONFLICT`, `POSSIBLE DUPLICATE` or `AMBIGUOUS` instead of saving. |
| `reflect` | Record, support or challenge a working theory about you ("over-commits in spring"). |
| `amend` | Confirm, retract, pin, or merge two entries for the same person. |

## Why it doesn't forget

Most memory tools fail quietly: the model never calls them. AIF handles this in three ways.

1. **The tools describe when to use them.** The `recall` description includes your live map and
   the names of people on file, so the model can see what is there. The `remember` description
   lists the kinds of things worth saving and tells the model not to wait to be asked.
2. **The hooks catch what the model misses** (Claude Code, via `aif hooks install`):
   - When your message mentions someone on file ("my sister"), the model is told who that is and
     to look them up.
   - When your message sounds like a lasting fact ("I'm allergic to…", "from now on…", "my
     manager…"), the model is reminded to save it.
   - When the model finishes, if you shared something and nothing was saved, it gets one reminder.
     It never gets a second reminder about the same thing.
3. **A new memory asks to be filled.** While the store is nearly empty, the capsule tells models to
   learn the basics about you as conversations allow.

For other agents, `aif setup` prints a short rules block for `AGENTS.md`, `.cursorrules` or
custom instructions.

## Trust

- **Every fact records who said it.** A fact counts as *you said it* only when the model includes
  your actual words. A model's guess stays a guess. One model repeating itself doesn't raise its
  confidence. Several different models agreeing does, but never to the level of something you said.
- **Nothing is overwritten.** The log is append-only. Changing or retracting a fact adds a new
  entry, so the old value is still there. Each entry is content-hashed, and `aif verify` checks for
  tampering.
- **Contradictions are flagged.** If a model tries to record a second employer, it gets a
  `CONFLICT` back and asks you which one is current.
- **It knows time passes.** A sister who was 11 two years ago is now about 13. A school recorded two
  years ago is marked as possibly out of date.

## Everyday commands

| command | |
|---|---|
| `aif init` | create your memory |
| `aif edit` / `aif edit persona` | edit who you are / how your AI should be |
| `aif remember TOPIC KEY VALUE` | add a fact, e.g. `aif remember food diet vegetarian` |
| `aif note TOPIC TEXT` | add a free-form note |
| `aif recall QUERY` | search |
| `aif show` | read everything as one page |
| `aif people` | everyone you've mentioned |
| `aif beliefs` | what models currently think about you |
| `aif forget ID` / `aif confirm ID` / `aif pin ID` | fix things up |
| `aif review` | a checklist of stale, unconfirmed or conflicting items |
| `aif capsule` | print the summary models see |
| `aif setup` | config for every supported client |
| `aif hooks install` | add the Claude Code hooks |
| `aif export` / `aif import` | move data out, or bring in another assistant's memory export |
| `aif sync` | sync with a git remote |

Run `aif --help` or `aif COMMAND --help` for every option.

## More than one device

Your memory is a git repo at `~/.aif` (set `AIF_HOME` to move it). Push it to a **private**
remote and run `aif sync` on each machine. Entries merge without conflicts.

You can also run one shared server and point every device at it:

```bash
AIF_TOKEN=$(openssl rand -hex 32) aif serve --http 0.0.0.0:8765
```

```bash
claude mcp add --transport http aif http://SERVER:8765/mcp --header "Authorization: Bearer $AIF_TOKEN"
```

Only expose it to the internet behind HTTPS.

## Compatibility

The MCP server supports protocol versions 2024-11-05 through 2026-07-28. That covers clients that
open with `initialize` and newer stateless clients that call `server/discover`. It runs over stdio
or Streamable HTTP.

## Your data

Everything lives in `~/.aif`:

```
~/.aif/
  core.md       who you are, in your words
  persona.md    how your AI should be with you
  events/       every fact, note and change, one JSON line each
  capsule.md    the summary models see (regenerated automatically)
  AIF.md        instructions for any model that opens this folder
```

These are plain files you can read, grep, back up or delete. `AIF.md` explains the format to
whatever model or program finds the folder, and [`SPEC.md`](SPEC.md) is short enough to
re-implement in an afternoon. The point is that your memory outlives this tool.

## Limits

- The hook that spots facts worth saving is pattern-based and English-only. It errs toward
  staying quiet.
- `evals/when_to_call.py` measures how often models actually call `recall`. It needs API
  credentials and hasn't been run against current models yet.
- Search is keyword-based (BM25 plus the map's vocabulary), with no embeddings. That works well at
  personal scale, but it won't match synonyms the store has never seen.

## Development

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 evals/run.py
```

[`docs/DESIGN.md`](docs/DESIGN.md) explains the design choices. [`evals/RESULTS.md`](evals/RESULTS.md)
covers token cost over ten simulated years, conflict handling, and people-over-time checks.

MIT license.
