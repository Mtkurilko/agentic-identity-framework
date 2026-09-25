"""Derived view: replay the event log into facts and notes, and score them.

Nothing here is stored. The graph is rebuilt from events on load, so a better
scoring function later is just a code change, and old data picks it up for free.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from aif.events import SENSITIVITY, belief_id, fact_key, note_id, parse_time, validate

# Days for salience to halve without reinforcement. Inferences fade fastest.
HALF_LIFE = {"U": 365.0, "X": 270.0, "I": 90.0}
IMPORTANCE = {1: 0.35, 2: 0.6, 3: 0.9}
# However many models agree, an inference alone never reaches "established".
INFERRED_CEILING = 0.79
ESTABLISHED = 0.8
# Each inferred witness counts for at most this much on its own.
INFERRED_MAX_SINGLE = 0.6

# Predicates that hold one value at a time unless a `define` event says otherwise.
DEFAULT_SINGLE = frozenset("""
name preferred_name pronouns birthday age lives_in hometown timezone employer role job_title
school relationship_status partner diet native_language primary_os phone editor shell car
""".split())
DEFAULT_PRIVATE_TOPICS = frozenset({"health", "medical", "finance", "money", "legal"})
# On any subject other than `me`, these describe *who or what* the subject is (see entities.py).
RESERVED_PREDICATES = frozenset({"name", "kind", "relation_to_me", "alias"})


def is_identity(f) -> bool:
    return f.s != "me" and f.p in RESERVED_PREDICATES


def sens_rank(level: str) -> int:
    return SENSITIVITY.index(level) if level in SENSITIVITY else 1


def visible(item, clearance: str) -> bool:
    """Within clearance, or pinned: the user chose to always share it (never for secret)."""
    if sens_rank(item.sens) <= sens_rank(clearance):
        return True
    return bool(getattr(item, "pinned", False)) and item.sens != "secret"


@dataclass
class Fact:
    key: str
    s: str
    p: str
    o: str
    topic: str
    card: str = "many"
    sens: str = "personal"
    pinned: bool = False
    imp: int = 2
    since: str | None = None
    until: str | None = None
    stale_after: int | None = None  # days until the detail should be re-checked
    closed_at: str | None = None
    close_reason: str | None = None
    closed_by: str | None = None
    first_t: str = ""
    last_t: str = ""
    mentions: int = 0
    witnesses: dict = field(default_factory=dict)  # "src:contributor" -> {"src", "by", "conf"}
    evidence: list = field(default_factory=list)
    derived_from: list = field(default_factory=list)
    events: list = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.closed_at is None and not self.until

    @property
    def src(self) -> str:
        kinds = {w["src"] for w in self.witnesses.values()}
        return "U" if "U" in kinds else "X" if "X" in kinds else "I"

    @property
    def contributors(self) -> list[str]:
        return sorted({w["by"] for w in self.witnesses.values()})

    @property
    def conf(self) -> float:
        by_src = defaultdict(list)
        for w in self.witnesses.values():
            by_src[w["src"]].append(w["conf"])
        if by_src["U"]:
            return max(by_src["U"])
        imported = max(by_src["X"], default=0.0)
        inferred = 0.0
        if by_src["I"]:
            # Noisy-OR over *distinct* contributors: independent agreement is evidence,
            # one model repeating itself is not (witnesses are keyed by contributor).
            miss = 1.0
            for c in by_src["I"]:
                miss *= 1.0 - min(c, INFERRED_MAX_SINGLE)
            inferred = min(1.0 - miss, INFERRED_CEILING)
        return round(max(imported, inferred), 3)

    @property
    def tentative(self) -> bool:
        return self.conf < ESTABLISHED

    def salience(self, now: datetime) -> float:
        if self.pinned:
            return 1.0
        age = max(0.0, (now - parse_time(self.last_t)).total_seconds() / 86400)
        decay = 0.5 ** (age / HALF_LIFE[self.src])
        reinforcement = min(1.5, 1.0 + 0.15 * math.log1p(max(0, self.mentions - 1)))
        return min(1.0, IMPORTANCE.get(self.imp, 0.6) * decay * reinforcement)

    def score(self, now: datetime) -> float:
        return self.conf * self.salience(now)


@dataclass
class Note:
    id: str
    t: str
    topic: str
    text: str
    src: str
    by: str
    title: str | None = None
    sens: str = "personal"
    about: str | None = None  # entity id the note is about
    retracted_at: str | None = None
    consolidated_into: str | None = None

    @property
    def open(self) -> bool:
        return self.retracted_at is None


# Beliefs: the model-side "world-view". Interpretations, patterns and working assumptions about the
# user or someone in their life. They are never facts: they carry a confidence that moves with
# evidence, and they can be revised or refuted, with the lineage kept.
BELIEF_CEILING = 0.85   # without the user's say-so, a belief stays a belief
BELIEF_PRIOR_MAX = 0.6  # a fresh hypothesis starts at or below this
BELIEF_HALF_LIFE = 180.0


@dataclass
class Belief:
    id: str
    about: str
    text: str
    t: str
    by: str
    prior: float
    kind: str | None = None
    src: str = "I"
    sens: str = "personal"
    basis: list = field(default_factory=list)
    support: list = field(default_factory=list)  # [{"t","by","src","reason","basis"}]
    against: list = field(default_factory=list)
    status: str = "open"  # open | confirmed | refuted | revised
    revises: str | None = None
    revised_by: str | None = None
    closed_at: str | None = None
    last_t: str = ""

    @property
    def open(self) -> bool:
        return self.status in ("open", "confirmed")

    @property
    def conf(self) -> float:
        if self.status == "confirmed":
            return 0.95
        if self.status == "refuted":
            return 0.02
        logit = math.log(self.prior / (1 - self.prior))
        seen: Counter = Counter()
        for s in self.support:
            seen[s["by"]] += 1
            # A new contributor noticing the same pattern counts fully; the same one again counts less.
            logit += 0.5 / seen[s["by"]]
        logit -= 0.8 * len(self.against)
        return round(min(BELIEF_CEILING, 1 / (1 + math.exp(-logit))), 3)

    @property
    def observations(self) -> int:
        return 1 + len(self.support) + len(self.against)

    def salience(self, now: datetime) -> float:
        age = max(0.0, (now - parse_time(self.last_t or self.t)).total_seconds() / 86400)
        return 0.5 ** (age / BELIEF_HALF_LIFE)

    def score(self, now: datetime) -> float:
        return self.conf * self.salience(now) * min(1.5, 1 + 0.1 * (self.observations - 1))


class Graph:
    def __init__(self):
        self.facts: dict[str, Fact] = {}
        self.notes: dict[str, Note] = {}
        self.beliefs: dict[str, Belief] = {}
        self.predicates: dict[str, dict] = {}
        self.problems: list[str] = []
        self.event_count = 0
        self.last_t = ""

    @classmethod
    def replay(cls, events) -> "Graph":
        graph = cls()
        seen = set()
        # Stable sort on time alone: same-second events keep their append order
        # (a confirm written right after its assert must replay after it).
        for ev in sorted(events, key=lambda e: e.get("t", "")):
            if ev.get("id") in seen:
                continue  # identical event arrived via two devices
            seen.add(ev.get("id"))
            graph.apply(ev)
        return graph

    # ---- replay -----------------------------------------------------------------

    def apply(self, ev: dict) -> None:
        problems = validate(ev)
        if problems:
            self.problems.extend(f"{ev.get('id', '?')}: {p}" for p in problems)
            if any("missing" in p or "unknown op" in p or p.startswith("bad") for p in problems):
                return
        self.event_count += 1
        self.last_t = max(self.last_t, ev["t"])
        getattr(self, "_op_" + ev["op"])(ev)

    def _op_define(self, ev):
        entry = self.predicates.setdefault(ev["p"], {})
        for k in ("card", "topic", "desc"):
            if ev.get(k):
                entry[k] = ev[k]

    def _op_assert(self, ev):
        key = fact_key(ev["s"], ev["p"], ev["o"])
        fact = self.facts.get(key)
        if fact is None:
            fact = self.facts[key] = Fact(key=key, s=ev["s"], p=ev["p"], o=ev["o"], topic=ev["topic"],
                                          first_t=ev["t"])
        elif not fact.open and not ev.get("until"):
            fact.closed_at = fact.close_reason = fact.closed_by = None  # re-asserted: reopen
            fact.until = None
        fact.o, fact.topic = ev["o"], ev["topic"]
        if ev.get("card"):
            self.predicates.setdefault(ev["p"], {})["card"] = ev["card"]
        fact.card = self.card_of(ev["p"])
        if ev.get("sens"):
            fact.sens = ev["sens"]
        fact.imp = max(fact.imp if fact.mentions else 1, int(ev.get("imp", 2)))
        self._witness(fact, ev)
        if ev.get("since"):
            fact.since = ev["since"]
        if ev.get("until"):
            fact.until = ev["until"]
        if ev.get("stale_after"):
            fact.stale_after = int(ev["stale_after"])
        if ev.get("pin") and ev["src"] == "U":
            fact.pinned = True
        for ref in ev.get("from", []):
            fact.derived_from.append(ref)
            if ref in self.notes:
                self.notes[ref].consolidated_into = key
        for old in ev.get("supersedes", []):
            target = self.facts.get(old)
            if target and target.open and old != key:
                target.closed_at, target.close_reason, target.closed_by = ev["t"], "superseded", key

    def _witness(self, fact: Fact, ev: dict):
        wkey = f"{ev['src']}:{ev.get('by', 'unknown')}"
        w = fact.witnesses.setdefault(wkey, {"src": ev["src"], "by": ev.get("by", "unknown"), "conf": 0.0})
        w["conf"] = max(w["conf"], float(ev.get("conf", 0.5)))
        fact.mentions += 1
        fact.last_t = max(fact.last_t, ev["t"])
        fact.events.append(ev["id"])
        if ev.get("ev") and ev["ev"] not in fact.evidence:
            fact.evidence = (fact.evidence + [ev["ev"]])[-3:]

    def _op_confirm(self, ev):
        target = self.find(ev["ref"])
        if isinstance(target, Fact):
            ev = dict(ev, conf=ev.get("conf", 0.95 if ev["src"] == "U" else 0.5))
            self._witness(target, ev)
        elif target is None:
            self.problems.append(f"{ev['id']}: confirm of unknown ref {ev['ref']}")

    def _op_retract(self, ev):
        target = self.find(ev["ref"])
        if isinstance(target, Fact) and target.open:
            target.closed_at, target.close_reason = ev["t"], ev.get("reason", "retracted")
        elif isinstance(target, Note) and target.open:
            target.retracted_at = ev["t"]

    def _op_pin(self, ev):
        target = self.find(ev["ref"])
        if isinstance(target, Fact) and ev["src"] == "U":
            target.pinned = True

    def _op_unpin(self, ev):
        target = self.find(ev["ref"])
        if isinstance(target, Fact):
            target.pinned = False

    def _op_note(self, ev):
        nid = note_id(ev)
        self.notes[nid] = Note(id=nid, t=ev["t"], topic=ev["topic"], text=ev["text"], src=ev["src"],
                               by=ev.get("by", "unknown"), title=ev.get("title"),
                               sens=ev.get("sens", "personal"), about=ev.get("about"))

    def _op_belief(self, ev):
        bid = belief_id(ev)
        prior = min(BELIEF_PRIOR_MAX, max(0.05, float(ev.get("conf", 0.4)))) if ev["src"] != "U" else 0.6
        b = self.beliefs[bid] = Belief(id=bid, about=ev["about"], text=ev["text"], t=ev["t"], by=ev["by"],
                                       prior=prior, kind=ev.get("kind"), src=ev["src"],
                                       sens=ev.get("sens", "personal"), basis=list(ev.get("basis", [])),
                                       revises=ev.get("revises"), last_t=ev["t"])
        if ev["src"] == "U":
            b.status = "confirmed"  # the user's own reading of themselves
        old = self.beliefs.get(ev.get("revises") or "")
        if old and old.open:
            old.status, old.revised_by, old.closed_at = "revised", bid, ev["t"]

    def _op_evidence(self, ev):
        b = self.beliefs.get(ev["ref"])
        if b is None or not b.open and ev["src"] != "U":
            if b is None:
                self.problems.append(f"{ev['id']}: evidence for unknown belief {ev['ref']}")
            return
        entry = {"t": ev["t"], "by": ev["by"], "src": ev["src"], "reason": ev.get("reason"),
                 "basis": ev.get("basis", []), "ev": ev.get("ev")}
        (b.support if ev["dir"] == "+" else b.against).append(entry)
        b.last_t = max(b.last_t, ev["t"])
        if ev["src"] == "U":  # the user settles it
            b.status = "confirmed" if ev["dir"] == "+" else "refuted"
            b.closed_at = ev["t"] if ev["dir"] == "-" else None

    # ---- queries ----------------------------------------------------------------

    def card_of(self, p: str) -> str:
        return self.predicates.get(p, {}).get("card") or ("one" if p in DEFAULT_SINGLE else "many")

    def find(self, ref: str):
        ref = ref.strip().strip("[]")
        return self.facts.get(ref) or self.notes.get(ref) or self.beliefs.get(ref)

    def slot(self, s: str, p: str) -> list[Fact]:
        """Open facts filling the same subject+predicate."""
        return [f for f in self.facts.values() if f.open and f.s == s and f.p == p]

    def visible(self, clearance: str):
        facts = [f for f in self.facts.values() if visible(f, clearance)]
        notes = [n for n in self.notes.values() if visible(n, clearance)]
        return facts, notes

    def topic_map(self, clearance: str) -> dict[str, dict]:
        """Per-topic inventory: what is on file, whether or not it is loaded."""
        out: dict[str, dict] = defaultdict(lambda: {"facts": 0, "notes": 0, "hidden": 0, "preds": Counter()})
        for f in self.facts.values():
            if not f.open or is_identity(f):
                continue
            entry = out[f.topic]
            if not visible(f, clearance):
                entry["hidden"] += 1
            else:
                entry["facts"] += 1
                entry["preds"][f.p] += 1
        for n in self.notes.values():
            if n.open and n.consolidated_into is None:
                if not visible(n, clearance):
                    out[n.topic]["hidden"] += 1
                else:
                    out[n.topic]["notes"] += 1
        return dict(out)

    def contested_slots(self) -> list[list[Fact]]:
        slots = defaultdict(list)
        for f in self.facts.values():
            if f.open and f.card == "one":
                slots[(f.s, f.p)].append(f)
        return [fs for fs in slots.values() if len(fs) > 1]
