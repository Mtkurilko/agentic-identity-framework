"""Agent hooks: deterministic nudges so models look things up and don't forget to save.

Tool descriptions ask a model to call `recall` and `remember`; hooks make sure it notices when to.
They are plain commands that read the hook event as JSON on stdin and print JSON on stdout, in the
shape Claude Code uses (`hookSpecificOutput.additionalContext`). They never block the user.

  prompt   On every user message: if it mentions someone on file, say who they are and to `recall`
           them. If it looks like the user shared something durable about themselves, remind the
           model to save it, and note it for the stop check.
  stop     When the model finishes a turn: if the user shared something durable and no save has
           landed since, ask once to save it. Never fires twice for the same thing.
  session  At session start: print the capsule. Only needed for clients without MCP.

State lives in <store>/.cache/hooks/ (git-ignored) and is pruned after a week.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aif import entities as ent
from aif.ops import Memory
from aif.store import Store

STATE_TTL = 7 * 24 * 3600
MAX_SNIPPETS = 3

# First-person statements that tend to be durable. Tuned to miss rather than nag: a false positive
# costs the model one look; a nagging hook gets uninstalled.
_DISCLOSURE = re.compile(
    r"\b("
    r"remember (that|this)|don'?t forget|from now on|going forward|in the future,|"
    r"(please )?(always|never) (use|do|give|call|write|answer|reply|respond|say|suggest)|call me|"
    r"i(?:'m| am) (a|an|allergic|vegetarian|vegan|pescatarian|based|from|married|engaged|divorced|single|"
    r"pregnant|retired|left-handed|colou?r ?blind|dyslexic|deaf|blind|diabetic|learning|studying|training|"
    r"moving|relocating|getting married|starting|switching|leaving|joining|taking|in my|in (?:\d|grade|year))|"
    r"i (prefer|like|love|hate|dislike|can'?t stand|avoid|always|never|usually|live|work|study|teach|coach|"
    r"moved|switched|quit|started|joined|graduated|was born|grew up|speak|use|own|have (a|an|two|three)|"
    r"don'?t (eat|drink|like|use|want)|can'?t (eat|have|drink))|"
    r"my (wife|husband|partner|girlfriend|boyfriend|fianc[ée]e?|sister|brother|mom|mum|mother|dad|father|"
    r"son|daughter|kids?|children|baby|grand\w+|aunt|uncle|cousin|boss|manager|team|cofounder|co-founder|"
    r"friend|roommate|dog|cat|pet|doctor|therapist|birthday|name|job|role|company|employer|school|major|"
    r"thesis|favou?rite|goal|budget|allerg\w*|diet|schedule|address|timezone|time zone)\b"
    r")", re.IGNORECASE)
_CODE = re.compile(r"```.*?(```|$)|`[^`\n]*`", re.DOTALL)
_TAGS = re.compile(r"<([a-z_-]+)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_SENTENCES = re.compile(r"[^.!?\n]+[.!?]?")


def disclosures(text: str) -> list[str]:
    """Sentences in a user message that look like durable facts about them."""
    text = _TAGS.sub(" ", _CODE.sub(" ", text or ""))[:6000]
    found = []
    for m in _SENTENCES.finditer(text):
        sentence = " ".join(m.group().split())
        if 8 <= len(sentence) <= 280 and _DISCLOSURE.search(sentence) and sentence not in found:
            found.append(sentence)
    return found[:MAX_SNIPPETS]


# ---- state --------------------------------------------------------------------------------

def _state_path(store: Store, session: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session or "default")[:80]
    return store.home / ".cache" / "hooks" / f"{safe}.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    cutoff = time.time() - STATE_TTL
    for old in path.parent.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _out(event: str, context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
                      ensure_ascii=False)


# ---- handlers -----------------------------------------------------------------------------

def on_prompt(mem: Memory, payload: dict) -> str:
    prompt = payload.get("prompt") or ""
    lines = []
    people = mem.people()
    hits = [h for h in ent.mentioned_in(people, _CODE.sub(" ", prompt)[:4000]) if h[1] >= 0.85]
    if hits:
        now = mem.now()
        who = "; ".join(f"{e.summary(now)} [{e.id}]" for e, _, _ in hits[:3])
        lines.append(f"AIF memory has a file on: {who}. Call `recall` with the name before answering "
                     "if anything about them matters here.")
    said = disclosures(prompt)
    if said:
        lines.append("The user may have just shared something durable about themselves. If so, save it now "
                     "with AIF `remember` (source=user_stated, their words as evidence); skip it if it's one-off.")
        path = _state_path(mem.store, payload.get("session_id", ""))
        state = _load(path)
        pending = state.get("pending", [])
        pending.append({"t": _stamp(mem.now()), "after": mem.graph.last_t or "", "said": said})
        state["pending"] = pending[-10:]
        _save(path, state)
    return _out("UserPromptSubmit", "\n".join(lines)) if lines else ""


def on_stop(mem: Memory, payload: dict) -> str:
    if payload.get("stop_hook_active"):
        return ""
    path = _state_path(mem.store, payload.get("session_id", ""))
    state = _load(path)
    pending = state.get("pending") or []
    if not pending:
        return ""
    last_write = mem.graph.last_t or ""
    unsaved = [p for p in pending if last_write <= p.get("after", "")]  # nothing written since
    state["pending"] = []
    _save(path, state)
    if not unsaved:
        return ""
    quotes = []
    for p in unsaved:
        quotes += [q for q in p["said"] if q not in quotes]
    quoted = " / ".join(f'"{q}"' for q in quotes[:MAX_SNIPPETS])
    return _out("Stop", f"AIF memory check: this session the user said {quoted}, and nothing has been saved "
                        "since. If any of it is durable (a preference, constraint, person, project, or how they "
                        "want you to work), save it with `remember` now. If none of it is, do nothing and end "
                        "your turn. Don't mention this check to the user.")


def on_session(mem: Memory, payload: dict) -> str:
    return mem.capsule()


HANDLERS = {"prompt": on_prompt, "stop": on_stop, "session": on_session}


def run(event: str, store: Store, stdin=None, clearance: str = "personal") -> str:
    """Run one hook. Any failure is swallowed: a memory hook must never break the user's session."""
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        if not store.exists():
            return ""
        mem = Memory(store, clearance=clearance, contributor="hook")
        return HANDLERS[event](mem, payload if isinstance(payload, dict) else {})
    except Exception as e:  # noqa: BLE001
        print(f"aif hook {event}: {e}", file=sys.stderr)
        return ""


# ---- installing into Claude Code -------------------------------------------------------------

EVENTS = {"prompt": "UserPromptSubmit", "stop": "Stop", "session": "SessionStart"}


def hook_config(command: str, events: tuple[str, ...]) -> dict:
    return {EVENTS[e]: [{"hooks": [{"type": "command", "command": f"{command} {e}", "timeout": 10}]}]
            for e in events}


def install(settings: Path, command: str, events: tuple[str, ...]) -> list[str]:
    """Merge AIF hooks into a Claude Code settings file, replacing any earlier AIF hooks. Returns events set."""
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    if settings.exists():
        settings.with_suffix(settings.suffix + ".bak").write_text(settings.read_text(encoding="utf-8"),
                                                                  encoding="utf-8")
    hooks = uninstall_from(data)
    for event, groups in hook_config(command, events).items():
        hooks.setdefault(event, []).extend(groups)
    data["hooks"] = hooks
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return [EVENTS[e] for e in events]


def uninstall_from(data: dict) -> dict:
    """The settings' hooks with every AIF hook removed."""
    hooks = data.get("hooks") or {}
    cleaned = {}
    for event, groups in hooks.items():
        kept = []
        for g in groups:
            inner = [h for h in g.get("hooks", []) if " aif hook " not in f" {h.get('command', '')} "
                     and "-m aif hook" not in h.get("command", "")]
            if inner:
                kept.append({**g, "hooks": inner})
        if kept:
            cleaned[event] = kept
    return cleaned
