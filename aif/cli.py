"""`aif` command line. The user is the author here, so CLI writes are source=user_stated."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from aif import FORMAT, __version__
from aif.events import SENSITIVITY
from aif.ops import Memory
from aif.store import Store

CLI_EVIDENCE = "(entered by the user via aif CLI)"


def _memory(args, clearance=None) -> Memory:
    store = Store(args.home)
    if not store.exists():
        sys.exit(f"No AIF store at {store.home}. Run `aif init` first (or set AIF_HOME).")
    return Memory(store, clearance=clearance or args.clearance or "personal", contributor="user-cli")


def _ask(question: str) -> str:
    try:
        return input(question + "\n> ").strip()
    except EOFError:
        return ""


def _onboard(store: Store) -> str | None:
    """Four questions on first run. Everything is optional and lands in plain files you can edit later."""
    print("A few quick questions (press Enter to skip any). You can change all of it later.\n")
    name = _ask("What should AI call you?")
    about = _ask("In a sentence or two: what do you do, and what are you working on?")
    style = _ask("How do you like answers? (e.g. short and direct, code first, explain like a teacher)")
    persona = _ask("How should your AI be with you? (tone, how blunt, when to push back, anything to never do)")
    always = _ask("Anything every AI should always know? (allergies, accessibility, hard rules)")
    core = [line for line in (f"Call me {name}." if name else "", about,
                              f"How I like answers: {style}" if style else "",
                              f"Always keep in mind: {always}" if always else "") if line]
    if core:
        store.core_path.write_text(store.core_path.read_text(encoding="utf-8").rstrip() + "\n\n"
                                   + "\n".join(core) + "\n", encoding="utf-8")
    if persona:
        store.persona_path.write_text(store.persona_path.read_text(encoding="utf-8").rstrip() + "\n\n"
                                      + persona + "\n", encoding="utf-8")
    return name or None


def cmd_init(args):
    store = Store(args.home)
    fresh = not store.exists()
    created = store.init(owner=args.owner)
    if fresh and not args.quick and sys.stdin.isatty():
        name = _onboard(store)
        if name and not args.owner:
            store.init(owner=name)
    mem = Memory(store, contributor="user-cli")
    store.write_capsule(mem.capsule())
    store.commit("aif: initialise store", all_files=True)
    print(f"\nAIF store ready at {store.home}" + (f" (created {', '.join(created)})" if created else ""))
    print("Next: connect your AI tools with `aif setup`. Edit yourself any time with `aif edit` / `aif edit persona`.")


def cmd_capsule(args):
    mem = _memory(args)
    text, stats = mem.capsule(budget=args.budget, with_stats=True)
    print(text, end="")
    if args.stats:
        print(json.dumps(stats), file=sys.stderr)


def cmd_recall(args):
    print(_memory(args).recall(" ".join(args.query), topic=args.topic, history=args.history, limit=args.limit))


def cmd_remember(args):
    mem = _memory(args)
    print(mem.remember(args.topic, args.predicate, " ".join(args.value), "user_stated", evidence=CLI_EVIDENCE,
                       subject=args.subject, single=True if args.single else (False if args.multi else None),
                       replace=args.replace, importance=args.importance, since=args.since,
                       sensitivity=args.sensitivity, pin=args.pin, force=args.force, subject_kind=args.kind,
                       subject_relation=args.relation, subject_aliases=args.alias, stale_after=args.stale_after))


def cmd_note(args):
    print(_memory(args).note(args.topic, " ".join(args.text), "user_stated", title=args.title,
                             evidence=CLI_EVIDENCE, sensitivity=args.sensitivity, subject=args.subject,
                             subject_relation=args.relation))


def cmd_beliefs(args):
    from aif.render import belief_line
    mem = _memory(args)
    g, names = mem.graph, mem._names()
    held = [b for b in g.beliefs.values() if args.all or b.open]
    if not held:
        print("(no beliefs yet; models add them with the `reflect` tool)")
    for b in sorted(held, key=lambda b: (not b.open, -b.conf)):
        print(belief_line(b, names))
    if held:
        print("\nconfirm or reject one yourself: aif confirm <id> / aif forget <id>")


def cmd_people(args):
    from aif.render import roster
    mem = _memory(args)
    print("\n".join(f"{line}" for line in roster(mem.people(), mem.now(), mem.clearance)) or "(nobody yet)")


def _amend(action):
    def run(args):
        print(_memory(args).amend(args.id, action, evidence=CLI_EVIDENCE, reason=args.reason))
    return run


def cmd_merge(args):
    print(_memory(args).merge(args.id, args.into, evidence=CLI_EVIDENCE, reason=args.reason))


def cmd_show(args):
    mem = _memory(args, clearance=args.clearance or "secret")
    text = mem.export_wiki()
    if args.topic:
        chunks = text.split("\n## ")
        text = "\n## ".join([chunks[0]] + [c for c in chunks[1:] if c.split("\n", 1)[0] == args.topic])
    print(text)


def cmd_map(args):
    print(_memory(args).map())


def cmd_review(args):
    print(_memory(args).review(), end="")


def cmd_status(args):
    store = Store(args.home)
    if not store.exists():
        print(f"No AIF store at {store.home}. Run `aif init`.")
        return
    mem = Memory(store, clearance=args.clearance or "personal")
    g = mem.graph
    _, stats = mem.capsule(with_stats=True)
    open_facts = [f for f in g.facts.values() if f.open]
    print(f"AIF {FORMAT} store: {store.home}" + (" (git)" if store.is_repo() else " (no git)"))
    print(f"owner: {store.config()['owner']} · events: {g.event_count} · current facts: {len(open_facts)} "
          f"(inferred: {sum(1 for f in open_facts if f.src == 'I')}, pinned: {sum(1 for f in open_facts if f.pinned)})"
          f" · notes: {sum(1 for n in g.notes.values() if n.open)} · topics: {len(g.topic_map('secret'))}")
    print(f"capsule: {stats['tokens']}/{stats['budget']} tokens · active {stats['active']} of "
          f"{stats['candidates']} candidates · core.md {stats['core_tokens']} tokens")
    problems = mem.verify()
    print(f"integrity: {'OK' if not problems else f'{len(problems)} problem(s), run `aif verify`'}")


def cmd_verify(args):
    problems = _memory(args).verify()
    if not problems:
        print("OK: every event matches its content hash, and git history shows no edits or deletions.")
        return
    print("\n".join(problems))
    sys.exit(1)


def cmd_export(args):
    mem = _memory(args, clearance=args.clearance or "secret")
    if args.format == "wiki":
        print(mem.export_wiki())
    elif args.format == "capsule":
        print(mem.capsule(), end="")
    else:
        events, _ = mem.store.read_events()
        for ev in sorted(events, key=lambda e: (e["t"], e["id"])):
            print(json.dumps(ev, ensure_ascii=False, sort_keys=True))


def cmd_import(args):
    mem = _memory(args)
    text = Path(args.file).read_text(encoding="utf-8") if args.file != "-" else sys.stdin.read()
    if args.format == "aif":
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        with mem.store.lock():
            known = {e["id"] for e in mem.store.read_events()[0]}
            fresh = [e for e in events if e.get("id") not in known]
            mem.store.append(fresh)
            mem.store.commit(f"aif: merge {len(fresh)} events from {Path(args.file).name}")
        print(f"merged {len(fresh)} new events ({len(events) - len(fresh)} already present)")
        return
    n = mem.import_lines(text.splitlines(), topic=args.topic, origin=args.origin)
    print(f"imported {n} notes into '{args.topic}' as source=imported. Ask a model to structure them "
          f"(`aif review`, or 'go through my {args.topic} memories and remember the durable ones').")


def cmd_edit(args):
    store = Store(args.home)
    if not store.exists():
        sys.exit(f"No AIF store at {store.home}. Run `aif init` first.")
    store.init()  # creates persona.md in stores made before it existed
    path = store.persona_path if args.which == "persona" else store.core_path
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
    os.system(f"{editor} {shlex.quote(str(path))}")
    mem = Memory(store, contributor="user-cli")
    if store.write_capsule(mem.capsule(clearance="personal")):
        store.commit(f"aif: edit {path.name}", all_files=True)
    print(f"saved {path}")


def cmd_hook(args):
    from aif.hooks import run
    out = run(args.event, Store(args.home), clearance=args.clearance or "personal")
    if out:
        print(out)


def _hook_command(home: str) -> str:
    return f"AIF_HOME={shlex.quote(home)} {shlex.quote(sys.executable)} -m aif hook"


def cmd_hooks(args):
    from aif.hooks import install, uninstall_from
    settings = Path(args.settings).expanduser()
    if args.action == "uninstall":
        if settings.exists():
            data = json.loads(settings.read_text(encoding="utf-8"))
            data["hooks"] = uninstall_from(data)
            if not data["hooks"]:
                del data["hooks"]
            settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"removed AIF hooks from {settings}")
        return
    events = ("prompt", "stop") + (("session",) if args.capsule else ())
    done = install(settings, _hook_command(str(Store(args.home).home)), events)
    print(f"installed AIF hooks ({', '.join(done)}) in {settings}")
    print("They surface people on file when you mention them, and catch facts you shared that never got saved.")


def cmd_sync(args):
    mem = _memory(args)
    store = mem.store
    if not store.is_repo():
        sys.exit("sync needs the store to be a git repo with a remote.")
    for step in (("pull", "--no-rebase", "--no-edit"), ("push",)):
        r = store._run_git(*step, check=False)
        if r.returncode != 0:
            sys.exit(f"git {step[0]} failed:\n{r.stderr.strip()}")
        if step[0] == "pull":
            if store.write_capsule(mem.capsule(clearance="personal")):
                store.commit("aif: re-render capsule after sync")
    print("synced")


def cmd_serve(args):
    from aif.mcp import Server

    store = Store(args.home)
    if not store.exists():
        store.init()
    clearance = args.clearance or os.environ.get("AIF_CLEARANCE", "personal")
    contributor = os.environ.get("AIF_CONTRIBUTOR", "unknown")
    if args.http:
        from aif.http_server import serve_http

        host, _, port = args.http.rpartition(":")
        token = os.environ.get("AIF_TOKEN")
        if args.token_file:
            token = Path(args.token_file).expanduser().read_text(encoding="utf-8").strip()
        if not token and not args.no_auth:
            sys.exit("aif serve --http needs a token: set AIF_TOKEN or --token-file (or pass --no-auth)")
        serve_http(store, host or "127.0.0.1", int(port), token=token or None, clearance=clearance,
                   contributor=contributor)
        return
    Server(Memory(store, clearance=clearance, contributor=contributor)).serve()


RULES = """Personal memory: I keep a portable memory (AIF) connected as the `aif` MCP server.
- Before assuming anything about me, my preferences, or people I mention, check the capsule and call `recall`.
- When I share something durable (a preference, constraint, person, project, or how I want you to work), \
save it with `remember` in the same turn. Don't wait to be asked.
- Follow the Persona section of my memory for how to work with me."""


def cmd_setup(args):
    cmd = [sys.executable, "-m", "aif", "serve"]
    home = str(Store(args.home).home)
    env = {"AIF_HOME": home}
    as_shell = " ".join(shlex.quote(c) for c in cmd)
    print(f"""# Connect AIF to your AI tools (store: {home})

## Claude Code
claude mcp add aif --scope user -e AIF_HOME={shlex.quote(home)} -- {as_shell}
aif hooks install        # recommended: nudges to recall people you mention and to save what you share

## Claude Desktop / Cursor / Windsurf / any MCP client (mcpServers JSON)
{json.dumps({"mcpServers": {"aif": {"command": cmd[0], "args": cmd[1:], "env": env}}}, indent=2)}

## OpenAI Codex CLI (~/.codex/config.toml)
[mcp_servers.aif]
command = "{cmd[0]}"
args = {json.dumps(cmd[1:])}
env = {{ AIF_HOME = "{home}" }}

## Any chat box with no tools
aif capsule | pbcopy     # then paste as your first message or custom instructions

## Optional: add to CLAUDE.md / AGENTS.md / .cursorrules / custom instructions
{RULES}
""")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aif", description="Portable, append-only personal memory for any AI model.")
    p.add_argument("--home", help="store directory (default $AIF_HOME or ~/.aif)")
    p.add_argument("--clearance", choices=SENSITIVITY,
                   help="highest sensitivity to reveal (default personal; show/export default secret)")
    p.add_argument("--version", action="version", version=f"aif {__version__} ({FORMAT})")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="create a store (asks a few questions the first time)")
    s.add_argument("--owner", help="your name")
    s.add_argument("--quick", action="store_true", help="skip the questions")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("capsule", help="print the budgeted context block (paste into any model)")
    s.add_argument("--budget", type=int)
    s.add_argument("--stats", action="store_true", help="print token stats to stderr")
    s.set_defaults(fn=cmd_capsule)

    s = sub.add_parser("recall", help="search memory: keywords, a topic, or an id")
    s.add_argument("query", nargs="*")
    s.add_argument("--topic")
    s.add_argument("--history", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_recall)

    s = sub.add_parser("remember", help="state a fact: aif remember food diet vegetarian --single")
    s.add_argument("topic")
    s.add_argument("predicate")
    s.add_argument("value", nargs="+")
    s.add_argument("--subject", default="me", help="who/what it's about: a name, nickname, 'my sister', or an id")
    s.add_argument("--relation", help="first mention of someone: how they relate to you (sister, manager, dog…)")
    s.add_argument("--kind", choices=("person", "pet", "place", "org", "thing", "project", "event"))
    s.add_argument("--alias", action="append", help="nickname (repeatable)")
    s.add_argument("--stale-after", type=int, help="days until this detail should be re-checked")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--single", action="store_true", help="predicate holds one value at a time")
    g.add_argument("--multi", action="store_true", help="predicate can hold many values")
    s.add_argument("--replace", action="store_true", help="this replaces the current value")
    s.add_argument("--importance", type=int, choices=(1, 2, 3), default=2)
    s.add_argument("--since")
    s.add_argument("--sensitivity", choices=SENSITIVITY)
    s.add_argument("--pin", action="store_true", help="always include (allergies, hard constraints)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_remember)

    s = sub.add_parser("note", help="freeform wiki note: aif note travel hates red-eye flights")
    s.add_argument("topic")
    s.add_argument("text", nargs="+")
    s.add_argument("--title")
    s.add_argument("--subject", help="who/what the note is about")
    s.add_argument("--relation")
    s.add_argument("--sensitivity", choices=SENSITIVITY)
    s.set_defaults(fn=cmd_note)

    for name, action, helptext in (("forget", "retract", "mark a fact/note no longer true (history kept)"),
                                   ("confirm", "confirm", "confirm a fact (e.g. a model's inference)"),
                                   ("pin", "pin", "always include a fact"), ("unpin", "unpin", "stop pinning")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("id")
        s.add_argument("--reason")
        s.set_defaults(fn=_amend(action))

    s = sub.add_parser("merge", help="fold a duplicate person/thing into another (same person, two entries)")
    s.add_argument("id", help="the duplicate (name or id)")
    s.add_argument("into", help="the one to keep")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("show", help="human-readable wiki of everything (optionally one topic)")
    s.add_argument("topic", nargs="?")
    s.set_defaults(fn=cmd_show)
    sub.add_parser("map", help="topics on file").set_defaults(fn=cmd_map)
    sub.add_parser("people", help="people, pets, places and things you've mentioned").set_defaults(fn=cmd_people)
    s = sub.add_parser("beliefs", help="models' working understanding of you (hypotheses)")
    s.add_argument("--all", action="store_true", help="include revised and refuted ones")
    s.set_defaults(fn=cmd_beliefs)
    sub.add_parser("review", help="maintenance worksheet: renewals, inferences, consolidation").set_defaults(fn=cmd_review)
    sub.add_parser("status", help="summary and health").set_defaults(fn=cmd_status)
    sub.add_parser("verify", help="integrity check (hashes + git history)").set_defaults(fn=cmd_verify)

    s = sub.add_parser("export", help="export as wiki markdown, capsule, or raw events")
    s.add_argument("--format", choices=("wiki", "capsule", "events"), default="wiki")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("import", help="import another assistant's memory export (lines) or another AIF log")
    s.add_argument("file", help="path, or - for stdin")
    s.add_argument("--format", choices=("lines", "aif"), default="lines")
    s.add_argument("--topic", default="imported")
    s.add_argument("--origin", default="import", help="where it came from, e.g. chatgpt-memory")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("edit", help="edit core.md (who you are) or persona.md (how your AI should be)")
    s.add_argument("which", nargs="?", choices=("core", "persona"), default="core")
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("hook", help="run an agent hook (reads the event JSON on stdin)")
    s.add_argument("event", choices=("prompt", "stop", "session"))
    s.set_defaults(fn=cmd_hook)
    s = sub.add_parser("hooks", help="install/uninstall AIF hooks in Claude Code settings")
    s.add_argument("action", choices=("install", "uninstall"))
    s.add_argument("--settings", default="~/.claude/settings.json", help="settings file (default user settings)")
    s.add_argument("--capsule", action="store_true", help="also inject the capsule at session start (no-MCP setups)")
    s.set_defaults(fn=cmd_hooks)

    sub.add_parser("sync", help="git pull (union merge) + re-render + push").set_defaults(fn=cmd_sync)
    s = sub.add_parser("serve", help="run the MCP server (stdio, or HTTP for many devices)")
    s.add_argument("--http", metavar="[HOST:]PORT", help="serve MCP over HTTP at http://HOST:PORT/mcp instead of stdio")
    s.add_argument("--token-file", help="file holding the bearer token clients must send (default $AIF_TOKEN)")
    s.add_argument("--no-auth", action="store_true", help="allow --http without a token (trusted networks only)")
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("setup", help="print config for Claude Code, Claude Desktop, Cursor, Codex…").set_defaults(fn=cmd_setup)

    args = p.parse_args(argv)
    (getattr(args, "fn", None) or cmd_status)(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
