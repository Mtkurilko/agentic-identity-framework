"""High-level memory operations shared by the CLI and the MCP server.

This is where the policy lives. Events and replay are mechanism; the rules for when
a write is allowed, when to ask instead of write, and how provenance gates trust are
all here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import re

from aif import entities as ent
from aif.events import ME, SENSITIVITY, belief_id, fact_key, norm, note_id, slug, source_code
from aif.graph import (BELIEF_PRIOR_MAX, DEFAULT_PRIVATE_TOPICS, INFERRED_MAX_SINGLE, Belief, Fact, Graph, Note,
                       is_identity, visible)
from aif.render import (belief_line, capsule, dossier, fact_line, map_inline, map_lines, month, note_line,
                        roster, wiki)
from aif.search import Index, jaccard
from aif.store import Store

DEFAULT_CONF = {"U": 0.95, "X": 0.8, "I": 0.5}
SELF_WORDS = {"me", "myself", "i", "user", "the user", "about me", "who am i"}
_NAME_LIKE = re.compile(r"[A-Z][\w'.-]*( [A-Z][\w'.-]*){0,2}")


class Memory:
    def __init__(self, store: Store, *, clearance: str = "personal", contributor: str = "unknown",
                 clock=None):
        if clearance not in SENSITIVITY:
            raise ValueError(f"clearance must be one of {SENSITIVITY}")
        self.store = store
        self.clearance = clearance
        self.contributor = contributor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._graph: Graph | None = None
        self._sig = None
        self.load_problems: list[str] = []

    # ---- state ------------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def _stamp(self) -> str:
        return self.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def graph(self) -> Graph:
        sig = self.store.signature()
        if self._graph is None or sig != self._sig:
            events, self.load_problems = self.store.read_events()
            self._graph, self._sig = Graph.replay(events), sig
        return self._graph

    def _visible(self, item) -> bool:
        return visible(item, self.clearance)

    def people(self) -> dict[str, ent.Entity]:
        g = self.graph
        stamp = (id(g), g.event_count)
        if getattr(self, "_people_stamp", None) != stamp:
            self._people, self._people_stamp = ent.build(g, self.now()), stamp
        return self._people

    def _names(self) -> dict[str, str]:
        return {e.id: e.name for e in self.people().values()}

    def _dossier(self, e: ent.Entity, limit: int = 30, heading: str | None = None) -> str:
        return dossier(e, self.now(), clearance=self.clearance, limit=limit, heading=heading, graph=self.graph,
                       people=self.people())

    def self_dossier(self, limit: int = 40) -> str:
        g = self.graph
        me = ent.Entity(id=ME, name=self.store.config()["owner"],
                        facts=[f for f in g.facts.values() if f.s == ME and f.open and not is_identity(f)],
                        past=[f for f in g.facts.values() if f.s == ME and not f.open],
                        beliefs=[b for b in g.beliefs.values() if b.about == ME and b.open])
        for e in self.people().values():
            for src, p, f in e.links_in:
                if src == ME:
                    me.links_out.append((p, e.id, f))
        return self._dossier(me, limit, heading=f"{me.name}, the user [me]: top {limit} facts by relevance; "
                                                "recall a topic for more")

    def _commit(self, events: list[dict], message: str) -> list[dict]:
        with self.store.lock():
            graph = self.graph  # also picks up writes made by other processes
            sealed = self.store.append(events)
            if all(ev["t"] >= graph.last_t for ev in sealed):
                # Patch, don't rebuild: new events sort last, so applying them is identical to replay.
                for ev in sealed:
                    graph.apply(ev)
                self._sig = self.store.signature()
            else:
                self._graph = None  # clock skew: fall back to a full replay
            self.store.write_capsule(self.capsule(clearance="personal"))
            self.store.commit(message)
        return sealed

    # ---- reads ------------------------------------------------------------------

    def capsule(self, budget: int | None = None, clearance: str | None = None, with_stats: bool = False):
        cfg = self.store.config()
        day = self.now().replace(hour=0, minute=0, second=0, microsecond=0)  # cache-stable within a day
        return capsule(self.graph, core=self.store.core(), now=day, budget=budget or cfg["budget"],
                       clearance=clearance or self.clearance, with_stats=with_stats)

    def map(self) -> str:
        lines = map_lines(self.graph, self.clearance, max_topics=10_000)
        return "\n".join(lines) if lines else "(memory is empty)"

    def recall(self, query: str = "", topic: str | None = None, history: bool = False, limit: int = 12) -> str:
        """Find anything relevant: an item by id, the user's profile, a person or thing (by name, nickname,
        relation, or description like 'the kid I coach'), and any matching facts, notes and beliefs."""
        g = self.graph
        query = (query or "").strip()
        if not query and not topic:
            who = roster(self.people(), self.now(), self.clearance)
            return ("Topics on file (count — top predicates):\n" + self.map()
                    + ("\nPeople & things: " + " · ".join(who) if who else ""))
        item = g.find(query) if query else None
        if item is not None:
            return self._detail(item)
        if norm(query) in SELF_WORDS or norm(query) == norm(self.store.config()["owner"]):
            return self.self_dossier()
        about, covered = self._about_entities(query) if query and not topic else (None, set())
        current, past, hidden = self._search(query, topic, history, exclude=covered)
        if about is not None:
            extra = current[:6]
            return about + ("\n\nalso relevant:\n" + "\n".join(extra) if extra else "")
        header = f'{len(current)} match{"es" if len(current) != 1 else ""}'
        header += f' for "{query}"' if query else ""
        header += f" in {slug(topic)}" if topic else ""
        shown = current[:limit]
        out = [header + (f" (showing {limit})" if len(current) > limit else "") + ":"] + shown
        if past and (history or len(shown) < limit):
            out += ["past:"] + past[: max(3, limit - len(shown))]
        if hidden:
            out.append(f"({hidden} private item(s) not shown at clearance '{self.clearance}'. "
                       "Ask the user; they can run `aif recall` locally.)")
        if not current and not past and not hidden:
            out.append("Nothing on file. Ask the user rather than guessing, and `remember` the answer if durable.")
            out.append("Topics: " + map_inline(g, self.clearance))
        return "\n".join(out)

    def _search(self, query: str, topic: str | None, history: bool, exclude: set = frozenset()):
        """Keyword search over facts, notes and beliefs. Returns (current lines, past lines, hidden count)."""
        g, now, names = self.graph, self.now(), self._names()
        topic = slug(topic) if topic else None
        facts = [f for f in g.facts.values() if (history or f.open) and (not topic or f.topic == topic)
                 and not is_identity(f) and f.s not in exclude]
        notes = [n for n in g.notes.values() if n.open and (not topic or n.topic == topic)
                 and n.about not in exclude and n.id not in exclude]
        beliefs = [b for b in g.beliefs.values() if b.open and not topic and b.about not in exclude]
        items = {f.key: f for f in facts} | {n.id: n for n in notes} | {b.id: b for b in beliefs}
        if query:
            docs = [(f.key, f"{f.topic} {f.p} {f.o} {names.get(f.s, f.s)} {' '.join(f.evidence)}") for f in facts]
            docs += [(n.id, f"{n.topic} {n.title or ''} {n.text}") for n in notes]
            docs += [(b.id, f"belief {b.kind or ''} {names.get(b.about, b.about)} {b.text}") for b in beliefs]
            ranked = [i for i, _ in Index(docs).search(query)]
            # A query that names a topic behaves like a topic filter, too.
            order = ranked + sorted({i for i, x in items.items()
                                     if getattr(x, "topic", None) == slug(query)} - set(ranked))
        else:
            order = [f.key for f in sorted(facts, key=lambda f: (-f.open, -f.score(now), f.key))]
            order += [n.id for n in sorted(notes, key=lambda n: n.t, reverse=True)]
        hidden = sum(1 for i in order if not self._visible(items[i]))
        current, past = [], []
        for i in order:
            x = items[i]
            if not self._visible(x):
                continue
            if isinstance(x, Fact):
                (current if x.open else past).append(fact_line(x, now, names))
            elif isinstance(x, Note):
                current.append(note_line(x))
            else:
                current.append("belief " + belief_line(x, names))
        return current, past, hidden

    def _about_entities(self, query: str) -> tuple[str | None, set]:
        """Who or what the query is about: resolved directly, or by description, or not at all.

        Returns (text, ids already covered) or (None, set()) when no entity is involved.
        """
        now, people = self.now(), self.people()
        hits = [h for h in ent.mentioned_in(people, query) if self._visible(h[0])]
        strong = [h for h in hits if h[1] >= 0.85]
        if strong and ent.ambiguous(strong):
            tied = [h for h in strong if strong[0][1] - h[1] < 0.1]
            out = [f"AMBIGUOUS: {query!r} could mean " + " or ".join(f"{ent.label(e)} [{e.id}]" for e, _, _ in tied)
                   + ". If the conversation doesn't already make it clear, ask one short question "
                     f"(\"{' or '.join(ent.label(e) for e, _, _ in tied)}?\"). Both, briefly:"]
            out += [self._dossier(e, limit=8) for e, _, _ in tied]
            return "\n".join(out), {e.id for e, _, _ in tied}
        if strong:
            e = strong[0][0]
            out = [self._dossier(e)]
            out += [f"also mentioned: {o.summary(now)} [{o.id}]" for o, _, _ in strong[1:3]]
            return "\n".join(out), {e.id} | {o.id for o, _, _ in strong[1:3]}

        if _NAME_LIKE.fullmatch(query) and len(query.split()) <= 2:
            # A proper name we have no profile for: say where it comes up instead of guessing someone else.
            notes, near = ent.mentions_of(self.graph, people, query)
            notes = [n for n in notes if self._visible(n)]
            if notes:
                out = [f"{query} has no profile yet, but comes up in {len(notes)} note(s):"]
                out += [f"- {note_line(n, max_chars=200)}" for n in notes[-5:]]
                if near:
                    out.append("mentioned alongside: " + ", ".join(f"{ent.label(e)} [{e.id}]" for e in near))
                out.append(f"If {query} matters here, use the notes; to remember them properly, introduce them: "
                           f"remember(subject={query!r}, subject_relation=..., ...).")
                return "\n".join(out), {n.id for n in notes}
        # No name, nickname or relation matched: search everyone's whole profile for the description.
        cands, terms = ent.describe(people, self.graph, query, now, allowed=self._visible)
        verdict = ent.verdict(cands)
        if verdict == "likely":
            c = cands[0]
            others = [o for o in cands[1:4] if o.coverage >= 0.5]
            out = [f"LIKELY: {query!r} is probably {ent.label(c.entity)} [{c.entity.id}]. Why: "
                   + "; ".join(c.why) + ". This is an inference: go with it if the conversation agrees; if acting "
                   f"on it matters, confirm in passing (\"you mean {c.entity.name}?\"). Once confirmed, save the "
                   f"description as an alias: remember(subject={c.entity.id!r}, subject_aliases=[{query!r}], ...).",
                   self._dossier(c.entity)]
            out += [f"less likely: {ent.label(o.entity)} [{o.entity.id}]: " + "; ".join(o.why[:2]) for o in others]
            return "\n".join(out), {c.entity.id}
        if verdict == "possible":
            top = [c for c in cands if c.coverage >= 0.5][:4]
            out = [f"POSSIBLE MATCHES for {query!r}, none certain:"]
            for c in top:
                out.append(f"- {c.entity.summary(now)} [{c.entity.id}], matched {c.coverage:.0%} of the "
                           f"description: " + "; ".join(c.why[:3]))
            names = " or ".join(c.entity.name for c in top[:2])
            out.append(f"If context makes one clear, recall its id for the full file. Otherwise ask one short "
                       f"question (\"{names}?\" or \"who do you mean?\").")
            return "\n".join(out), set()
        if hits:  # a weak direct match (e.g. "my sibling" and a sister on file)
            e, _, why = hits[0]
            return (f"Best match for {query!r}: {ent.label(e)} (matched by {why}). If you're not sure it's the same "
                    "one, confirm with the user in passing.\n" + self._dossier(e), {e.id})
        rel = re.search(r"\b(?:my|our)\s+(?:\w+\s+)?(\w+)", norm(query))
        if rel and slug(rel.group(1)) in ent.RELATION_WORDS:
            word = ent.canon_relation(rel.group(1)).replace("_", " ")
            return (f"No {word} on file, and nothing in the profiles fits. If it matters, ask the user once for "
                    f"their name, then save it: remember(subject=<name>, subject_relation={word!r}, ...)."), set()
        if people and any(t in ("minor", "adult", "senior") or t in ent.RELATION_CANON for t in terms):
            return (f"Nobody on file fits {query!r}. Ask the user who they mean (once), then save them with "
                    f"remember(subject=<name>, subject_relation=..., subject_aliases=[{query!r}]) so it resolves "
                    "next time."), set()
        return None, set()

    def _detail(self, item) -> str:
        if not self._visible(item):
            return f"[{item.key if isinstance(item, Fact) else item.id}] is private at this clearance."
        if isinstance(item, Note):
            return note_line(item, full=True)
        if isinstance(item, Belief):
            return self._belief_detail(item)
        lines = [fact_line(item, self.now())]
        for w in item.witnesses.values():
            lines.append(f"  witness: {w['src']} {w['conf']:.2f} by {w['by']}")
        lines += [f'  evidence: "{e}"' for e in item.evidence]
        if item.closed_by:
            lines.append(f"  superseded by [{item.closed_by}]")
        if item.derived_from:
            lines.append(f"  consolidated from: {', '.join(item.derived_from)}")
        return "\n".join(lines)

    # ---- writes -----------------------------------------------------------------

    def remember(self, topic: str, predicate: str | None, value: str, source: str, *,
                 evidence: str | None = None, subject: str = ME, single: bool | None = None,
                 replace: bool = False, importance: int = 2, since: str | None = None,
                 until: str | None = None, sensitivity: str | None = None, pin: bool = False,
                 confidence: float | None = None, model: str | None = None,
                 derived_from: list[str] | None = None, force: bool = False, subject_kind: str | None = None,
                 subject_relation: str | None = None, subject_aliases=None, stale_after: int | None = None) -> str:
        if not predicate:
            return self.note(topic, value, source, evidence=evidence, model=model, sensitivity=sensitivity,
                             subject=subject, subject_kind=subject_kind, subject_relation=subject_relation,
                             subject_aliases=subject_aliases)
        src, notes = source_code(source), []
        value = str(value).strip()
        topic, predicate = slug(topic), slug(predicate)
        if not value:
            return "ERROR: empty value."
        # "my sister is Rebecca" arrives as me.sister=Rebecca. The relationship stays an ordinary fact about the
        # user (so it has history: partner=Sam → partner=Jordan), and Rebecca gets an identity of her own.
        obj_intro, obj_msg = [], ""
        if (subject in (None, "", ME) and ent.canon_relation(predicate) in ent.RELATION_CANON
                and _NAME_LIKE.fullmatch(value)):
            rel = ent.canon_relation(predicate)
            same = [e for e in self.people().values()
                    if norm(value) == norm(e.name) or norm(value) in {norm(a) for a in e.aliases}]
            fits = [e for e in same if rel in {ent.canon_relation(r) for r in e.relations} or not e.relations]
            if same and not fits:  # a *different* person who shares a known name: keep them apart
                subject, subject_relation, value, predicate = value, predicate, None, None
            elif not same:
                r = self._subject(value, ent.kind_for_relation(predicate), None, None, source, evidence, model)
                if isinstance(r, str):
                    return r
                _, obj_intro, obj_msg = r
        resolved = self._subject(subject, subject_kind, subject_relation, subject_aliases, source, evidence, model)
        if isinstance(resolved, str):
            return resolved
        subject, intro, intro_msg = resolved
        intro, intro_msg = intro + obj_intro, "; ".join(m for m in (intro_msg, obj_msg) if m)
        if predicate is None:  # the introduction was the whole message
            if intro:
                self._commit(intro, f"[by:{model or self.contributor}] introduce {subject}")
            e = self.people().get(subject)
            return intro_msg or (f"already known: {e.summary(self.now())} [{subject}]" if e else "nothing to save")
        if src == "U" and not (evidence or "").strip():
            src = "I"
            notes.append("no evidence quote given, so saved as inferred. Pass the user's words in `evidence` "
                         "to record it as user_stated.")
        conf = DEFAULT_CONF[src] if confidence is None else max(0.0, min(1.0, float(confidence)))
        if src == "I":
            conf = min(conf, INFERRED_MAX_SINGLE)
        if pin and src != "U":
            pin = False
            notes.append("pin ignored: only the user's own statements can be pinned.")
        g = self.graph
        key = fact_key(subject, predicate, value)
        existing = g.facts.get(key)
        card = "one" if single else ("many" if single is False else g.card_of(predicate))
        supersedes = []

        if card == "one" and not (existing and existing.open):
            rivals = [f for f in g.slot(subject, predicate) if f.key != key]
            strong = [f for f in rivals if not (f.src == "I" and f.tentative)]
            if strong and not replace:
                on_file = "; ".join(fact_line(f) for f in strong)
                return (f"CONFLICT, not saved. `{predicate}` holds one value and the file says: {on_file}. "
                        f"Ask the user in one short question whether {value!r} replaces it. If it does, call "
                        f"remember again with replace=true and their words as evidence. If both are true, "
                        f"call again with single=false.")
            if strong and src != "U":
                return ("REFUSED: replacing a stated fact needs the user's own words (source=user_stated "
                        "with evidence). Ask them.")
            supersedes = [f.key for f in rivals]
        elif card == "many" and not existing and not force and not intro:
            dupes = [f for f in g.facts.values() if f.open and f.s == subject and f.key != key
                     and ((f.p == predicate and jaccard(f.o, value) >= 0.5) or jaccard(f.o, value) >= 0.8)]
            if dupes:
                best = dupes[0]
                return (f"POSSIBLE DUPLICATE, not saved: {fact_line(best)}. If it's the same thing, call "
                        f"remember with value={best.o!r} and predicate={best.p!r} to reinforce it; if it's "
                        f"genuinely different, call again with force=true.")

        known = sorted({f.topic for f in g.facts.values()} | {n.topic for n in g.notes.values()})
        new_topic = topic not in known
        if sensitivity is None and topic in DEFAULT_PRIVATE_TOPICS:
            sensitivity = "private"
        if sensitivity == "secret" and self.clearance != "secret":
            return "REFUSED: only the local CLI may write secret-level facts."
        by = model or self.contributor
        ev = {"t": self._stamp(), "op": "assert", "s": subject, "p": predicate, "o": value, "topic": topic,
              "src": src, "by": by, "conf": round(conf, 3), "ev": evidence if src == "U" else None,
              "imp": importance if importance in (1, 3) else None, "since": since, "until": until,
              "sens": sensitivity, "pin": True if pin else None, "card": "one" if single else None,
              "supersedes": supersedes or None, "from": derived_from or None,
              "stale_after": int(stale_after) if stale_after else None}
        before = existing.conf if existing else None
        who = "" if subject == ME else f"{subject}."
        self._commit(intro + [ev], f"[by:{by}] [{src} {conf:.2f}] {topic}/{who}{predicate}={value}"[:120])
        fact = self.graph.facts[key]
        if before is not None:
            msg = f"reinforced [{key}] {predicate}={value}: conf {before:.2f} → {fact.conf:.2f}"
            if fact.src == "I" and fact.conf >= 0.7:
                msg += " (several models agree; confirm with the user to establish it)"
        else:
            label = predicate if subject == ME else f"{self._names().get(subject, subject)}.{predicate}"
            msg = f"saved [{key}] {topic} · {label}={value} ({fact.src} {fact.conf:.2f})"
        if intro_msg:
            msg = intro_msg + "\n" + msg
        if supersedes:
            msg += f"; closed {', '.join('[' + k + ']' for k in supersedes)} (kept as history)"
        known = [t for t in known if t != "people"]
        if new_topic and topic != "people" and known:
            if known:
                msg += f". New topic {topic!r}; existing: {', '.join(known[:20])}"
        return "\n".join([msg] + [f"note: {n}" for n in notes])

    def note(self, topic: str, text: str, source: str, *, title: str | None = None, evidence: str | None = None,
             model: str | None = None, sensitivity: str | None = None, subject: str | None = None,
             subject_kind: str | None = None, subject_relation: str | None = None, subject_aliases=None) -> str:
        src = source_code(source)
        if src == "U" and not (evidence or "").strip():
            src = "I"
        resolved = self._subject(subject, subject_kind, subject_relation, subject_aliases, source, evidence, model)
        if isinstance(resolved, str):
            return resolved
        about, intro, intro_msg = resolved
        topic = slug(topic)
        if sensitivity is None and topic in DEFAULT_PRIVATE_TOPICS:
            sensitivity = "private"
        by = model or self.contributor
        ev = {"t": self._stamp(), "op": "note", "topic": topic, "text": str(text).strip(), "title": title,
              "src": src, "by": by, "sens": sensitivity, "about": None if about == ME else about}
        sealed = self._commit(intro + [ev], f"[by:{by}] [{src} note] {topic}: {str(text).strip()[:60]}")
        msg = f"saved note [{note_id(sealed[-1])}] in {topic}" + ("" if about == ME else f" about {about}")
        return (intro_msg + "\n" + msg) if intro_msg else msg

    def _subject(self, subject, kind, relation, aliases, source, evidence, model):
        """Resolve who a write is about. Returns (entity id, introduction events, message) or a string to relay.

        Known names, nicknames and relations ("my sister") resolve to the existing entity. A new name
        creates one. A bare relation with no name, or a relation that fits two people, is sent back
        as a question instead of guessed.
        """
        if subject is None or norm(subject) in SELF_WORDS or subject == ME:
            return ME, [], ""
        people, raw = self.people(), str(subject).strip()
        aliases = [aliases] if isinstance(aliases, str) else list(aliases or [])
        relation = slug(relation) if relation else None
        if relation is None and slug(raw) in ent.RELATION_WORDS:
            relation = slug(raw)  # "Mom" is both what they're called and who they are
        hits = ent.resolve(people, raw)
        strong = [h for h in hits if h[1] >= 0.85]
        if strong and ent.ambiguous(strong):
            names = " or ".join(f"{ent.label(e)} [{e.id}]" for e, _, _ in strong[:3])
            return (f"AMBIGUOUS, not saved: {raw!r} could be {names}. Ask the user which one, then call again "
                    f"with subject set to that id.")
        target = strong[0][0] if strong else None
        if target and relation and target.relations and strong[0][2] in ("name", "first name") and \
                ent.canon_relation(relation) not in {ent.canon_relation(r) for r in target.relations}:
            target = None  # same name, different relationship: a different person (two Rebeccas)
        if target is None and not _NAME_LIKE.fullmatch(raw) and len(raw.split()) > 1:
            cands, _ = ent.describe(people, self.graph, raw, self.now())
            verdict = ent.verdict(cands)
            if verdict != "none":
                c = cands[0]
                return (f"UNSURE, not saved: {raw!r} is {'probably' if verdict == 'likely' else 'possibly'} "
                        f"{ent.label(c.entity)} [{c.entity.id}] ({'; '.join(c.why[:2])}). If the user agrees, call "
                        f"again with subject={c.entity.id!r} (and subject_aliases=[{raw!r}] so it resolves next time).")
        if target is None and re.match(r"(my|our|his|her|their|the|a|an)\s", raw.lower()):
            rest = raw.split(None, 1)[1]
            if slug(rest) in ent.RELATION_WORDS:
                rel = ent.canon_relation(rest)
                return (f"UNKNOWN, not saved: no {rel.replace('_', ' ')} on file yet. Ask the user for their name "
                        f"(once), then call again with subject=<name> and subject_relation={rel!r}.")
            return (f"UNKNOWN, not saved: nobody on file fits {raw!r}. Ask the user who they mean (once), then call "
                    f"again with subject=<name>, subject_relation=... and subject_aliases=[{raw!r}].")
        src = source_code(source)
        if src == "U" and not (evidence or "").strip():
            src = "I"
        base = {"t": self._stamp(), "op": "assert", "topic": "people", "src": src,
                "by": model or self.contributor, "conf": DEFAULT_CONF[src] if src != "I" else 0.5,
                "ev": evidence if src == "U" else None}
        intro, said = [], []
        if target is None:
            sid = slug(raw)
            if sid in people or sid == ME:
                sid = f"{sid}_{relation or 'other'}"
            kind = kind or ent.kind_for_relation(relation)
            intro.append({**base, "s": sid, "p": "name", "o": raw})
            said.append(f"new {kind or 'entry'}: {raw} [{sid}]")
            clash = people.get(slug(raw))
            if clash:
                said.append(f"kept separate from {clash.summary(self.now())} [{clash.id}]")
        else:
            sid = target.id
            kind = kind if kind and not target.kind else None
        if kind and (target is None or not target.kind):
            intro.append({**base, "s": sid, "p": "kind", "o": slug(kind)})
        if relation and (target is None or ent.canon_relation(relation) not in
                         {ent.canon_relation(r) for r in target.relations}):
            intro.append({**base, "s": sid, "p": "relation_to_me", "o": relation})
            said.append(f"{raw} is your {relation.replace('_', ' ')}")
        known_aliases = {norm(a) for a in (target.aliases if target else [])}
        for a in aliases:
            if norm(a) not in known_aliases and norm(a) != norm(raw):
                intro.append({**base, "s": sid, "p": "alias", "o": str(a).strip()})
                said.append(f"aka {a}")
        return sid, intro, ("; ".join(said) if said else "")

    def _belief_detail(self, b: Belief) -> str:
        names, g = self._names(), self.graph
        lines = [belief_line(b, names), f"  proposed {b.t[:10]} by {b.by} (prior {b.prior:.2f})"
                 + (f", kind: {b.kind}" if b.kind else "")]
        for sign, entries in (("+", b.support), ("-", b.against)):
            lines += [f"  {sign} {e['t'][:10]} {e['by']}: " + (f'user said "{e["ev"]}"' if e.get("ev")
                                                               else e.get("reason") or "") for e in entries]
        if b.basis:
            lines.append("  based on: " + ", ".join(b.basis))
        chain, cur = [], g.beliefs.get(b.revises or "")
        while cur is not None and len(chain) < 5:
            chain.append(f"[{cur.id}] {cur.text}")
            cur = g.beliefs.get(cur.revises or "")
        if chain:
            lines.append("  revised from: " + " ← ".join(chain))
        if b.revised_by:
            lines.append(f"  revised into [{b.revised_by}]")
        return "\n".join(lines)

    # ---- world-view -------------------------------------------------------------

    def reflect(self, action: str, *, text: str | None = None, about: str | None = None, id: str | None = None,
                reason: str | None = None, basis: list[str] | None = None, evidence: str | None = None,
                confidence: float | None = None, kind: str | None = None, model: str | None = None) -> str:
        """Maintain the working understanding: hypotheses that go beyond stated facts, revised by evidence."""
        g, by = self.graph, model or self.contributor
        words = (evidence or "").strip()
        src = "U" if words else "I"
        if action == "propose":
            if not (text or "").strip():
                return "ERROR: propose needs `text`: the hypothesis, in one sentence."
            subject = ME
            if about and norm(about) not in SELF_WORDS and about != ME:
                hits = [h for h in ent.resolve(self.people(), about) if h[1] >= 0.85]
                if not hits or ent.ambiguous(hits):
                    return (f"UNKNOWN subject {about!r}: introduce them with remember(subject=..., subject_relation=...)"
                            " first, or use their [id].")
                subject = hits[0][0].id
            similar = [b for b in g.beliefs.values() if b.open and b.about == subject and jaccard(b.text, text) >= 0.5]
            if similar:
                b = similar[0]
                return (f"SIMILAR, not saved: {belief_line(b, self._names())}. If your observation backs it, call "
                        f"reflect(action='support', id={b.id!r}, reason=...). If it's a better formulation, "
                        f"reflect(action='revise', id={b.id!r}, text=...).")
            prior = min(BELIEF_PRIOR_MAX, float(confidence) if confidence is not None else 0.4)
            ev = {"t": self._stamp(), "op": "belief", "about": subject, "text": text.strip(), "kind": kind,
                  "src": src, "by": by, "conf": round(prior, 3), "basis": basis or None,
                  "reason": reason, "ev": words or None}
            sealed = self._commit([ev], f"[by:{by}] [{src} belief] {subject}: {text.strip()[:70]}")
            b = self.graph.beliefs[belief_id(sealed[0])]
            return f"holding [{b.id}] {b.text} ({b.conf:.2f}{', confirmed by user' if b.status == 'confirmed' else ''})"
        b = g.beliefs.get((id or "").strip("[]"))
        if b is None:
            return f"ERROR: no belief {id!r}. Use recall to find belief ids (they start with 'b')."
        if action in ("support", "contradict"):
            if not (reason or words):
                return "ERROR: say what you observed in `reason` (or pass the user's words as `evidence`)."
            before = b.conf
            ev = {"t": self._stamp(), "op": "evidence", "ref": b.id, "dir": "+" if action == "support" else "-",
                  "src": src, "by": by, "reason": reason or None, "basis": basis or None, "ev": words or None}
            self._commit([ev], f"[by:{by}] [{src}] {action} [{b.id}]: {(reason or words)[:60]}")
            b = self.graph.beliefs[b.id]
            state = {"confirmed": " (confirmed by the user)", "refuted": " (refuted by the user; kept as history)"}
            return f"{action}ed [{b.id}]: {before:.2f} → {b.conf:.2f}{state.get(b.status, '')}"
        if action == "revise":
            if not (text or "").strip():
                return "ERROR: revise needs the new `text`."
            prior = min(BELIEF_PRIOR_MAX, b.conf if confidence is None else float(confidence))
            ev = {"t": self._stamp(), "op": "belief", "about": b.about, "text": text.strip(), "kind": kind or b.kind,
                  "src": src, "by": by, "conf": round(max(0.05, prior), 3), "revises": b.id,
                  "reason": reason, "ev": words or None, "basis": basis or None}
            sealed = self._commit([ev], f"[by:{by}] revise [{b.id}]: {text.strip()[:60]}")
            new = self.graph.beliefs[belief_id(sealed[0])]
            return f"revised [{b.id}] → [{new.id}] {new.text} ({new.conf:.2f}); the old reading is kept as history"
        return "ERROR: action must be propose, support, contradict or revise."

    def amend(self, ref: str, action: str, *, evidence: str | None = None, reason: str | None = None,
              model: str | None = None) -> str:
        item = self.graph.find(ref)
        if item is None:
            return f"ERROR: no fact or note with id {ref!r}. Use recall to find ids."
        by = model or self.contributor
        has_words = bool((evidence or "").strip())
        if isinstance(item, Belief):
            if action not in ("confirm", "retract"):
                return "ERROR: beliefs can be confirmed or retracted (refuted) by the user; use reflect otherwise."
            if not has_words:
                return ("REFUSED: confirming or refuting a belief records the *user's* verdict, so pass their words "
                        "as evidence. For your own observations use reflect(support|contradict).")
            return self.reflect("support" if action == "confirm" else "contradict", id=item.id, evidence=evidence,
                                reason=reason, model=model)
        ref = item.key if isinstance(item, Fact) else item.id
        base = {"t": self._stamp(), "ref": ref, "by": by}
        if action == "confirm":
            if not isinstance(item, Fact):
                return "ERROR: only facts can be confirmed."
            if not has_words:
                return ("REFUSED: confirm records the *user's* confirmation, so pass their words as evidence. "
                        "If you only re-observed it yourself, call remember with source=inferred.")
            ev = {**base, "op": "confirm", "src": "U", "conf": 0.95, "ev": evidence}
        elif action == "retract":
            if isinstance(item, Fact) and item.src == "U" and not has_words:
                return "REFUSED: retracting something the user stated needs their words as evidence."
            ev = {**base, "op": "retract", "reason": reason or "retracted", "ev": evidence}
        elif action == "pin":
            if not has_words:
                return "REFUSED: pinning needs the user's explicit request as evidence."
            ev = {**base, "op": "pin", "src": "U", "ev": evidence}
        elif action == "unpin":
            ev = {**base, "op": "unpin"}
        else:
            return "ERROR: action must be confirm, retract, pin or unpin."
        self._commit([ev], f"[by:{by}] {action} [{ref}]" + (f": {reason}" if reason else ""))
        after = self.graph.find(ref)
        return f"{action}ed [{ref}]" + (f": now {fact_line(after)}" if isinstance(after, Fact) else "")

    # ---- maintenance ------------------------------------------------------------

    def review(self) -> str:
        """A maintenance worksheet any model (or the user) can work through."""
        g, now = self.graph, self.now()
        out = ["# AIF review",
               "Work through this with the user in ONE compact message, then record answers with "
               "amend(confirm|retract) or remember. Skip empty sections.", ""]
        faded = sorted((f for f in g.facts.values() if f.open and f.src == "U" and not f.pinned
                        and f.salience(now) < 0.3 and self._visible(f)), key=lambda f: f.last_t)[:8]
        if faded:
            out.append("## Renewal: still true? (established but not mentioned in a long time)")
            out += [f"- {fact_line(f)} last touched {month(f.last_t)}" for f in faded] + [""]
        pending = sorted((f for f in g.facts.values() if f.open and f.src == "I" and self._visible(f)),
                         key=lambda f: (-f.conf, f.key))[:8]
        if pending:
            out.append("## Unconfirmed inferences: ask before anyone relies on these")
            out += [f"- {fact_line(f)}" for f in pending] + [""]
        people, names = self.people(), self._names()
        ages = {e.id: (e.age(now) or (None,))[0] for e in people.values()}
        stale = [(f, ent.staleness(f, now, ages.get(f.s), g)) for f in g.facts.values()
                 if f.open and not is_identity(f) and self._visible(f)]
        stale = sorted(((f, st) for f, st in stale if st), key=lambda x: x[0].last_t)[:10]
        if stale:
            out.append("## Possibly outdated: time-sensitive details recorded a while ago")
            out += [f"- {fact_line(f, now, names)} ({st})" for f, st in stale] + [""]
        open_beliefs = [b for b in g.beliefs.values() if b.status == "open" and self._visible(b)]
        worth_asking = sorted((b for b in open_beliefs if b.observations >= 3 or b.against),
                              key=lambda b: (-bool(b.support and b.against), -b.observations))[:6]
        if worth_asking:
            out.append("## Beliefs worth checking with the user: well-observed or disputed hypotheses")
            out += [f"- {belief_line(b, names)}" for b in worth_asking] + [""]
        contested = g.contested_slots()
        if contested:
            out.append("## Conflicts: single-valued, multiple current values (e.g. from two devices)")
            for slot in contested:
                out.append("- " + " vs ".join(f"{f.o} [{f.key}]" for f in slot) + f" ({slot[0].p})")
            out.append("")
        by_topic: dict[str, list[Note]] = {}
        for n in g.notes.values():
            if n.open and n.consolidated_into is None and self._visible(n):
                by_topic.setdefault(n.topic, []).append(n)
        heavy = {t: ns for t, ns in by_topic.items() if len(ns) >= 5}
        if heavy:
            out.append("## Consolidate: summarise each cluster into 1–3 facts with remember(..., "
                       "derived_from=[note ids]). Notes stay in the archive.")
            for t, ns in sorted(heavy.items()):
                ns.sort(key=lambda n: n.t)
                out.append(f"### {t}: {len(ns)} notes, {month(ns[0].t)}..{month(ns[-1].t)}")
                out += [f"- {note_line(n, max_chars=160)}" for n in ns[:25]]
            out.append("")
        stats = self.capsule(with_stats=True)[1]
        core_budget = self.store.config()["core_budget"]
        if stats["core_tokens"] > core_budget:
            out.append(f"## Oversized core.md: {stats['core_tokens']} tokens > {core_budget}. Move details into facts.")
        if len(out) == 3:
            out.append("Nothing to review. Memory is in good shape.")
        return "\n".join(out).rstrip() + "\n"

    def verify(self) -> list[str]:
        """Integrity problems: edited lines, malformed events, history rewrites."""
        _ = self.graph
        return self.load_problems + self._graph.problems + [
            f"append-only violation in git history: {c}" for c in self.store.history_deletions()]

    def export_wiki(self) -> str:
        """The whole 'encyclopedia of you': your understanding, the people in your life, then every topic."""
        g, names = self.graph, self._names()
        owner = self.store.config()["owner"]
        text = wiki(g, owner=owner, now=self.now(), clearance=self.clearance)
        head, _, body = text.partition("\n\n")
        parts = [head, ""]
        beliefs = sorted((b for b in g.beliefs.values() if self._visible(b)), key=lambda b: (not b.open, -b.conf))
        if beliefs:
            parts.append("## Understanding (models' working hypotheses, with their history)")
            parts += [f"- {belief_line(b, names)}" for b in beliefs] + [""]
        people = sorted(self.people().values(), key=lambda e: (-(e.weight + 2 * bool(e.relations)), e.name))
        people = [e for e in people if self._visible(e)]
        if people:
            parts.append("## People & things")
            parts += [self._dossier(e) + "\n" for e in people]
        return "\n".join(parts) + "\n" + body

    def import_lines(self, lines: list[str], *, topic: str = "imported", origin: str = "import") -> int:
        """Import freeform memories (e.g. another assistant's memory export) as X-sourced notes.

        Structuring them into facts is left to a model: run `aif review` or ask one to go through
        the imported topic. Nothing is guessed at import time.
        """
        stamp = self._stamp()
        events = [{"t": stamp, "op": "note", "topic": slug(topic), "text": line.strip(" -*•\t"),
                   "src": "X", "by": origin} for line in lines if line.strip(" -*•\t")]
        if events:
            self._commit(events, f"[by:{origin}] import {len(events)} notes into {slug(topic)}")
        return len(events)

