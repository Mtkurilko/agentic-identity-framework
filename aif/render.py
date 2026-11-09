"""Text renderings: the capsule (what gets injected), the map, recall results, the wiki.

The capsule is built for three properties:
  * bounded: a hard token budget, filled best-first. It is not a threshold, so size stays flat forever.
  * self-decoding: the rules for reading and writing travel inside the text itself.
  * cache-stable: ordering is by topic/predicate, not by score, and time is bucketed
    to the day. The text only changes when *membership* changes, so provider
    prompt caches stay warm and git diffs stay minimal.
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict
from datetime import datetime

from aif.events import ME
from aif import entities as ent
from aif.graph import ESTABLISHED, Fact, Graph, Note, is_identity, sens_rank, visible

try:  # optional, exact counts for OpenAI-family tokenizers
    import tiktoken

    _ENC = tiktoken.get_encoding("o200k_base")
except Exception:  # pragma: no cover - depends on environment
    _ENC = None

MIN_SCORE = 0.05  # below this a fact is too faded to be worth any tokens
MAP_MAX_TOPICS = 16
MAP_PRED_HINTS = 5
ROSTER_MAX = 15
BELIEFS_MAX = 6
BELIEFS_SHARE = 0.15  # of the capsule budget
BELIEF_FLOOR = 0.3  # evidence has pushed it to "probably not": keep it searchable, not injected

_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]")


def estimate_tokens(text: str) -> int:
    """Token count: exact if tiktoken is installed, else a BPE-shaped heuristic (±15%)."""
    if _ENC is not None:
        return len(_ENC.encode(text))
    n = 0
    for m in _TOKEN_RE.finditer(text):
        w = m.group()
        if w[0].isalpha():
            n += max(1, math.ceil(len(w) / 6)) if len(w) > 4 else 1
        elif w[0].isdigit():
            n += math.ceil(len(w) / 3)
        else:
            n += 1
    return n


def month(t: str | None) -> str:
    return (t or "")[:7]


def _value(f: Fact, marks: dict | None = None) -> str:
    v = f.o + ("?" if f.tentative else "")
    mark = (marks or {}).get(f.key)
    return f"{v} ({mark})" if mark else v


def _label(f: Fact, names: dict | None = None) -> str:
    return f.p if f.s == ME else f"{(names or {}).get(f.s, f.s)}.{f.p}"


def inline_group(facts: list[Fact], names: dict | None = None, marks: dict | None = None,
                 subject_prefix: bool = True) -> str:
    """`p=v1, v2 · q=v3` with multi-valued predicates folded together."""
    slots: OrderedDict[str, list[str]] = OrderedDict()
    for f in sorted(facts, key=lambda f: (f.s != ME, f.s, f.p, f.o.lower())):
        label = _label(f, names) if subject_prefix else f.p
        slots.setdefault(label, []).append(_value(f, marks))
    return " · ".join(f"{label}={', '.join(vals)}" for label, vals in slots.items())


def topic_block(facts: list[Fact], names: dict | None = None, marks: dict | None = None) -> str:
    by_topic: dict[str, list[Fact]] = {}
    for f in facts:
        by_topic.setdefault(f.topic, []).append(f)
    return "\n".join(f"{t}: {inline_group(fs, names, marks)}" for t, fs in sorted(by_topic.items()))


def time_marks(graph: Graph, people: dict, now: datetime, detailed: bool = False) -> dict[str, str]:
    """fact key -> '(as of …)' for details that have probably changed since they were recorded."""
    ages = {e.id: (e.age(now) or (None,))[0] for e in people.values()}
    marks = {}
    for f in graph.facts.values():
        if f.open:
            stale = ent.staleness(f, now, ages.get(f.s), graph)
            if stale:
                marks[f.key] = stale if detailed else stale.split(",")[0]
    return marks


def roster(people: dict, now: datetime, clearance: str) -> list[str]:
    shown = sorted((e for e in people.values() if (e.facts or e.relations) and visible(e, clearance)),
                   key=lambda e: (-(e.weight + 2 * bool(e.relations)), e.name))
    parts = [e.summary(now) for e in shown[:ROSTER_MAX]]
    if len(shown) > ROSTER_MAX:
        parts.append(f"+{len(shown) - ROSTER_MAX} more (recall a name)")
    return parts


def map_lines(graph: Graph, clearance: str, max_topics: int = MAP_MAX_TOPICS) -> list[str]:
    inventory = graph.topic_map(clearance)
    ranked = sorted(inventory.items(), key=lambda kv: (-(kv[1]["facts"] + kv[1]["notes"] + kv[1]["hidden"]), kv[0]))
    lines = []
    for topic, e in ranked[:max_topics]:
        parts = [f"{topic} {e['facts']}"]
        hints = [p for p, _ in e["preds"].most_common(MAP_PRED_HINTS)]
        if hints:
            more = "…" if len(e["preds"]) > MAP_PRED_HINTS else ""
            parts.append("— " + ", ".join(hints) + more)
        if e["notes"]:
            parts.append(f"+{e['notes']} notes")
        if e["hidden"]:
            parts.append(f"(+{e['hidden']} private: ask the user first)")
        lines.append(" ".join(parts))
    if len(ranked) > max_topics:
        lines.append(f"…+{len(ranked) - max_topics} more topics (recall with no arguments lists all)")
    return lines


def map_inline(graph: Graph, clearance: str, max_topics: int = 12) -> str:
    inventory = graph.topic_map(clearance)
    ranked = sorted(inventory.items(), key=lambda kv: -(kv[1]["facts"] + kv[1]["notes"] + kv[1]["hidden"]))
    out = []
    for topic, e in ranked[:max_topics]:
        hints = ", ".join(p for p, _ in e["preds"].most_common(3))
        total = e["facts"] + e["notes"] + e["hidden"]
        out.append(f"{topic}({total}{': ' + hints if hints else ''})")
    if len(ranked) > max_topics:
        out.append(f"+{len(ranked) - max_topics} more")
    return " · ".join(out) if out else "(empty so far)"


def spot_check(graph: Graph, now: datetime, clearance: str) -> Fact | None:
    """One established fact that has faded: worth re-verifying if it comes up naturally."""
    limit = sens_rank(clearance)
    faded = [f for f in graph.facts.values()
             if f.open and not f.pinned and not is_identity(f) and f.src == "U" and f.conf >= ESTABLISHED
             and sens_rank(f.sens) <= limit and f.salience(now) < 0.3]
    return min(faded, key=lambda f: (-f.imp, f.last_t, f.key)) if faded else None


HEADER = """# About the user: AIF memory capsule ({date})
The user's portable long-term memory (aif/1). "?" = a model's unconfirmed guess. "(as of …)" = recorded long ago; may have changed.
PEOPLE & THINGS are who/what the user talks about: `recall` a name, a relation ("my sister") or a description ("the kid I coach") for their full file. MAP lists what else is on file. Before assuming anything about the user or their world, check here and `recall`; if nothing is on file, ask.
UNDERSTANDING is models' working read of the user: hypotheses, not facts. Use it to anticipate, never to overrule the user; keep it current with `reflect`.
Ask at most one short question, and only when the answer changes what you'd do (a mention fits two people, or an old detail matters to this task). Otherwise just use what you know. Save durable new facts with `remember`. Don't edit this text."""


PERSONA_TOPIC = "persona"
NEW_STORE_FACTS = 3  # fewer stated facts than this, and no core.md: the model should get to know the user
NEW_STORE = """## New memory
This memory is nearly empty. As the conversation allows (never as a questionnaire), learn and `remember` what to call the user, what they do or are working on, and how they want you to work with them. If they'd like to set it up properly, offer the `get_to_know_me` interview."""


def _is_persona(f: Fact) -> bool:
    return f.topic == PERSONA_TOPIC and f.s == "me"


def capsule(graph: Graph, *, core: str, now: datetime, budget: int, clearance: str = "personal",
            with_stats: bool = False, persona: str = ""):
    limit = sens_rank(clearance)
    people = ent.build(graph, now)
    names = {e.id: e.name for e in people.values()}
    marks = time_marks(graph, people, now)
    sections = [HEADER.format(date=now.strftime("%Y-%m-%d"))]
    core = core.strip()
    if core:
        sections.append("## Core\n" + core)
    persona_facts = sorted((f for f in graph.facts.values()
                            if f.open and _is_persona(f) and sens_rank(f.sens) <= limit),
                           key=lambda f: (f.p, f.key))
    persona = persona.strip()
    if persona or persona_facts:
        body = "\n".join(filter(None, [persona, " · ".join(f"{f.p}={_value(f, marks)}" for f in persona_facts)]))
        sections.append("## Persona (how the user wants their AI to be; follow it)\n" + body)
    if not core and sum(1 for f in graph.facts.values() if f.open and f.src == "U" and f.s == "me") < NEW_STORE_FACTS:
        sections.append(NEW_STORE)
    pinned = [f for f in graph.facts.values()
              if f.open and f.pinned and f.sens != "secret" and not is_identity(f) and not _is_persona(f)]
    if pinned:
        sections.append("## Always true\n" + topic_block(pinned, names, marks))
    who = roster(people, now, clearance)
    if who:
        sections.append("## People & things\n" + " · ".join(who))
    held = sorted((b for b in graph.beliefs.values()
                   if b.open and visible(b, clearance) and b.conf >= BELIEF_FLOOR),
                  key=lambda b: (-b.score(now), b.id))
    understanding, used_b = [], 0
    for b in held[:BELIEFS_MAX]:
        line = "- " + belief_line(b, names)
        cost = estimate_tokens(line)
        if used_b + cost > budget * BELIEFS_SHARE:
            continue
        understanding.append(line)
        used_b += cost
    if understanding:
        sections.append("## Understanding (working hypotheses; hold loosely)\n" + "\n".join(understanding))
    tail = []
    lines = map_lines(graph, clearance)
    if lines:
        tail.append("## Map (topic count: top predicates; recall for details)\n" + "\n".join(lines))
    check = spot_check(graph, now, clearance)
    if check:
        tail.append(f"## Check\nIf it comes up naturally, re-verify: {_label(check, names)}={check.o} "
                    f"(last confirmed {month(check.last_t)}) [{check.key}]")

    fixed = estimate_tokens("\n\n".join(sections + tail))
    remaining = budget - fixed - estimate_tokens("## Active\n")
    candidates = sorted(
        (f for f in graph.facts.values()
         if f.open and not f.pinned and not is_identity(f) and not _is_persona(f) and sens_rank(f.sens) <= limit
         and f.score(now) >= MIN_SCORE),
        key=lambda f: (-f.score(now), f.key))
    chosen: list[Fact] = []
    used, topics = 0, set()
    for f in candidates:
        cost = estimate_tokens(f"{_label(f, names)}={_value(f, marks)} · ")
        if f.topic not in topics:
            cost += estimate_tokens(f"\n{f.topic}: ")
        if used + cost > remaining:
            continue
        chosen.append(f)
        topics.add(f.topic)
        used += cost
    if chosen:
        sections.append("## Active\n" + topic_block(chosen, names, marks))
    text = "\n\n".join(sections + tail) + "\n"
    if not with_stats:
        return text
    return text, {
        "tokens": estimate_tokens(text), "budget": budget, "fixed_tokens": fixed,
        "active": len(chosen), "candidates": len(candidates), "pinned": len(pinned),
        "open_facts": sum(1 for f in graph.facts.values() if f.open), "entities": len(people),
        "beliefs": len(understanding),
        "core_tokens": estimate_tokens(core), "persona_tokens": estimate_tokens(persona),
    }


# ---- recall output ------------------------------------------------------------------

def fact_line(f: Fact, now: datetime | None = None, names: dict | None = None, stale: str | None = None) -> str:
    meta = [f"{f.src} {f.conf:.2f}".replace(" 0.", " .") + (" ?" if f.tentative else "")]
    if f.src == "I":
        meta.append(",".join(f.contributors))
    if f.open:
        meta.append(f"since {f.since}" if f.since else month(f.first_t))
    else:
        end = f.until or month(f.closed_at)
        meta.append(f"{f.since or month(f.first_t)}→{end}" + (f", {f.close_reason}" if f.close_reason else ""))
    if stale:
        meta.append(f"MAY BE OUTDATED: {stale}")
    if f.pinned:
        meta.append("pinned")
    if f.sens in ("private", "secret"):
        meta.append(f.sens)
    return f"[{f.key}] {f.topic} · {_label(f, names)}={f.o} ({' · '.join(meta)})"


def belief_line(b, names: dict | None = None) -> str:
    name = (names or {}).get(b.about, b.about)
    who = "" if b.about == ME or b.text.lower().startswith(name.lower()) else f"{name}: "
    status = {"confirmed": "confirmed by user", "refuted": "refuted", "revised": "revised"}.get(b.status)
    tally = f"+{len(b.support)}/-{len(b.against)}" if b.support or b.against else "new"
    meta = status or f"{b.conf:.2f}, {tally}"
    return f"[{b.id}] {who}{b.text} ({meta})"


def dossier(e: "ent.Entity", now: datetime, *, clearance: str, limit: int = 30, heading: str | None = None,
            graph: Graph | None = None, people: dict | None = None, beliefs: list | None = None) -> str:
    """An identity file: who/what they are, what's true now, how they connect, how they changed,
    what's been said about them, and what models currently believe about them."""
    age = e.age(now)
    names = {x.id: x.name for x in (people or {}).values()}
    marks = {}
    for f in e.facts:
        stale = ent.staleness(f, now, age[0] if age else None, graph)
        if stale:
            marks[f.key] = f"{stale} [{f.key}]"
        elif f.tentative:
            marks[f.key] = f"unconfirmed [{f.key}]"
    about = [e.kind] if e.kind else []
    about += [f"your {r.replace('_', ' ')}" for r in e.relations]
    if age:
        about.append(f"age {age[1]}")
    if e.aliases:
        about.append("aka " + ", ".join(e.aliases))
    lines = [heading or (f"{e.name} ({' · '.join(about)}) [{e.id}]" if about else f"{e.name} [{e.id}]")]
    facts = [f for f in e.facts if visible(f, clearance)]
    hidden = len(e.facts) - len(facts)
    facts = sorted(facts, key=lambda f: -f.score(now))[:limit]
    by_topic: dict[str, list[Fact]] = {}
    for f in facts:
        by_topic.setdefault(f.topic, []).append(f)
    for topic, fs in sorted(by_topic.items()):
        lines.append(f"  {topic}: {inline_group(fs, marks=marks, subject_prefix=False)}")
    links = [f"you {p.replace('_', ' ')} them" if src == ME else f"{names.get(src, src)} {p.replace('_', ' ')} them"
             for src, p, _ in e.links_in if src != ME or ent.canon_relation(p) not in
             {ent.canon_relation(r) for r in e.relations}]
    links += [f"{p.replace('_', ' ')} {names.get(dst, dst)} [{dst}]" for p, dst, _ in e.links_out]
    if links:
        lines.append("  connections: " + " · ".join(dict.fromkeys(links)))
    former = [f for f in e.former if f.p not in e.relations]
    if former:
        lines.append("  formerly: " + " · ".join(
            f"your {f.p.replace('_', ' ')} ({f.since or month(f.first_t)}→{f.until or month(f.closed_at)})"
            for f in former))
    past = [f for f in e.past if visible(f, clearance)]
    if past:
        changes = sorted(past, key=lambda f: f.closed_at or f.until or "")[-6:]
        lines.append("  earlier: " + " · ".join(
            f"{f.p}={f.o} ({f.since or month(f.first_t)}→{f.until or month(f.closed_at)})" for f in changes))
    notes = sorted((n for n in e.notes + e.mentions if visible(n, clearance)), key=lambda n: n.t)[-5:]
    lines += [f"  note {month(n.t)}: {n.text[:200]}" for n in notes]
    held = [b for b in (beliefs if beliefs is not None else e.beliefs) if b.open and visible(b, clearance)]
    for b in sorted(held, key=lambda b: -b.score(now))[:5]:
        lines.append(f"  believed: {belief_line(b)}")
    sources = sorted({w["by"] for f in facts for w in f.witnesses.values()})
    if sources:
        lines.append("  recorded by: " + ", ".join(sources))
    if hidden:
        lines.append(f"  (+{hidden} private detail(s) not shown)")
    if any("ago" in m for m in marks.values()):
        lines.append("  Some details are old. If one matters for this task, check it in passing (\"is she still "
                     "at …?\") and record the answer with amend(confirm) or remember(replace=true). If it doesn't "
                     "matter, don't ask.")
    return "\n".join(lines)


def note_line(n: Note, full: bool = False, max_chars: int = 320) -> str:
    text = n.text if full or len(n.text) <= max_chars else n.text[:max_chars].rstrip() + "… (recall this id for the rest)"
    title = f"{n.title}: " if n.title else ""
    meta = f"{n.src} · {month(n.t)}" + (f" · {n.sens}" if n.sens in ("private", "secret") else "")
    return f"[{n.id}] {n.topic} · note · {title}{text} ({meta})"


def wiki(graph: Graph, *, owner: str, now: datetime, clearance: str = "secret") -> str:
    """A human-readable 'wiki of you': every topic, current facts, history and notes."""
    facts, notes = graph.visible(clearance)
    topics = sorted({f.topic for f in facts} | {n.topic for n in notes})
    out = [f"# {owner}: personal wiki", f"_Generated {now:%Y-%m-%d} from the AIF event log. "
           f"{sum(1 for f in facts if f.open)} current facts, {len(notes)} notes._", ""]
    for topic in topics:
        out.append(f"## {topic}")
        current = sorted((f for f in facts if f.topic == topic and f.open), key=lambda f: (f.s, f.p, f.o))
        past = sorted((f for f in facts if f.topic == topic and not f.open), key=lambda f: f.first_t)
        for f in current:
            out.append(f"- {fact_line(f, now)}")
        if past:
            out.append("\n_History_")
            out.extend(f"- ~~{_label(f)}={f.o}~~ ({f.since or month(f.first_t)}→"
                       f"{f.until or month(f.closed_at)})" for f in past)
        topic_notes = sorted((n for n in notes if n.topic == topic and n.open),
                             key=lambda n: n.t)
        if topic_notes:
            out.append("\n_Notes_")
            out.extend(f"- {month(n.t)}: {(n.title + ': ') if n.title else ''}{n.text}" for n in topic_notes)
        out.append("")
    return "\n".join(out)
