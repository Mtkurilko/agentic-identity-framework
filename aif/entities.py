"""Entities: the people, pets, places and things in the user's life, and how they change.

An entity is any subject other than `me` that has facts about it. Its identity is carried by
ordinary facts with reserved predicates, so no new event type is needed:

    s=rebecca p=name           o=Rebecca
    s=rebecca p=kind           o=person
    s=rebecca p=relation_to_me o=sister
    s=rebecca p=alias          o=Becca

The code does the precise work a human memory can't: exact dates, projecting ages forward,
noticing that "middle school" was recorded two years ago. Deciding what that implies, and
whether it's worth asking about, is left to the model reading the output.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime

import statistics
from collections import defaultdict

from aif.events import ME, norm, parse_time, slug
from aif.graph import Belief, Fact, Graph, Note, RESERVED_PREDICATES
from aif.search import Index, tokenize

KINDS = ("person", "pet", "place", "org", "thing", "project", "event")

# Words that name a relationship, mapped to a canonical form. Near-synonyms share a group so
# "my wife" can still find someone recorded as "partner" (at lower confidence).
_REL_GROUPS = {
    "sister": "sister sis", "brother": "brother bro", "sibling": "sibling sister brother sis bro",
    "mother": "mother mom mum mommy mama", "father": "father dad daddy papa", "parent": "parent mother father",
    "partner": "partner wife husband spouse girlfriend boyfriend gf bf fiance fiancee",
    "son": "son", "daughter": "daughter", "child": "child kid son daughter",
    "grandmother": "grandmother grandma nana granny", "grandfather": "grandfather grandpa granddad",
    "aunt": "aunt auntie", "uncle": "uncle", "cousin": "cousin", "niece": "niece", "nephew": "nephew",
    "friend": "friend buddy pal bestie best_friend", "best_friend": "best_friend bff bestie",
    "manager": "manager boss", "coworker": "coworker colleague", "roommate": "roommate flatmate",
    "dog": "dog puppy pup", "cat": "cat kitty kitten", "pet": "pet dog cat",
    "doctor": "doctor dr physician", "therapist": "therapist", "teacher": "teacher",
    "player": "player", "student": "student pupil", "teammate": "teammate", "classmate": "classmate",
    "neighbor": "neighbor neighbour", "client": "client customer", "mentor": "mentor", "mentee": "mentee",
    "coach": "coach", "trainer": "trainer", "tutor": "tutor", "babysitter": "babysitter nanny",
    "landlord": "landlord", "tenant": "tenant",
}
_CANON: dict[str, str] = {}
for canon, words in _REL_GROUPS.items():
    for w in words.split():
        _CANON.setdefault(w, canon)
# A query word widens only its own meaning: "sibling" finds a sister, "sister" never finds a brother.
_WIDER = {canon: {_CANON[w] for w in words.split()} for canon, words in _REL_GROUPS.items()}
RELATION_WORDS = frozenset(_CANON)

# Days after the last confirmation when a detail is likely out of date. (Age is not here: it is
# projected forward from the date it was recorded, see Entity.age.)
STALE_AFTER = {
    "grade": 300, "school": 365, "class": 300, "year_in_school": 300, "wants": 180,
    "wishlist": 180, "current_project": 120, "project": 180, "reading": 90, "goal": 365,
    "employer": 730, "role": 730, "job_title": 730, "team": 540, "manager": 540, "clothing_size": 365,
    "shoe_size": 540, "lives_in": 1095, "address": 1095, "relationship_status": 540, "partner": 1095,
    "phone": 730, "car": 1460,
}
MINOR_INTEREST_STALE = 365  # a child's likes and hobbies change fast


RELATION_CANON = frozenset(_REL_GROUPS)
_PET_RELATIONS = {"dog", "cat", "pet"}


def kind_for_relation(relation: str | None) -> str | None:
    if not relation:
        return None
    c = canon_relation(relation)
    return "pet" if c in _PET_RELATIONS else "person" if c in RELATION_CANON else None


def canon_relation(word: str) -> str:
    w = slug(word)
    return _CANON.get(w, w)


@dataclass
class Entity:
    id: str
    name: str
    kind: str | None = None
    relations: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)  # open facts, reserved predicates excluded
    past: list[Fact] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)       # notes explicitly about them
    mentions: list[Note] = field(default_factory=list)    # other notes that name them
    links_out: list[tuple[str, str, Fact]] = field(default_factory=list)  # (predicate, other entity id, fact)
    links_in: list[tuple[str, str, Fact]] = field(default_factory=list)   # (other id or "me", predicate, fact)
    former: list[Fact] = field(default_factory=list)  # relationships to the user that ended (me.partner=…, closed)
    beliefs: list[Belief] = field(default_factory=list)
    last_t: str = ""
    weight: float = 0.0
    sens: str = "personal"

    def age(self, now: datetime) -> tuple[int, str] | None:
        """Current age, projected from whatever was recorded, and how it was derived."""
        for f in self.facts:
            if f.p in ("birthday", "born", "birth_date", "birth_year", "date_of_birth"):
                m = re.search(r"(19|20)\d\d(-\d\d(-\d\d)?)?", f.o)
                if m:
                    born = parse_time(m.group())
                    years = now.year - born.year - ((now.month, now.day) < (born.month, born.day))
                    exact = m.group(2) is not None
                    return years, ("" if exact else "≈") + f"{years} (born {m.group()})"
        for f in self.facts:
            if f.p == "age":
                m = re.search(r"\d+", f.o)
                if m:
                    said = parse_time(f.since or f.last_t)
                    elapsed = now.year - said.year - ((now.month, now.day) < (said.month, said.day))
                    years = int(m.group()) + max(0, elapsed)
                    return years, f"≈{years} ({m.group()} as of {f.last_t[:7]})"
        return None

    def summary(self, now: datetime) -> str:
        bits = [r.replace("_", " ") for r in self.relations] or ([self.kind] if self.kind else [])
        if self.kind and self.relations and self.kind != kind_for_relation(self.relations[0]):
            bits.append(self.kind)
        age = self.age(now)
        if age:
            bits.append(age[1].split(" (")[0])
        if self.aliases:
            bits.append("aka " + "/".join(self.aliases[:3]))
        return f"{self.name}: {', '.join(bits)}" if bits else self.name


def label(e: "Entity") -> str:
    """Name plus what disambiguates it: 'Rebecca (sister)'."""
    tag = e.relations[0].replace("_", " ") if e.relations else e.kind
    return f"{e.name} ({tag})" if tag else e.name


def shelf_lives(graph: Graph) -> dict[str, int]:
    """How long values of each predicate actually lasted in this user's history (learned, not assumed).

    Every superseded or retracted fact is a data point: `school` Oak Elementary 2019→2023 says a
    school lasts about four years *for this family*. With two or more data points, a detail is
    flagged once it reaches ~75% of the median lifetime. Cached per graph state.
    """
    cached = getattr(graph, "_shelf", None)
    if cached and cached[0] == graph.event_count:
        return cached[1]
    lives = defaultdict(list)
    for f in graph.facts.values():
        end = f.until or f.closed_at
        if f.open or not end or f.p in RESERVED_PREDICATES:
            continue
        try:
            days = (parse_time(end) - parse_time(f.since or f.first_t)).days
        except ValueError:
            continue
        if days > 0:
            lives[f.p].append(days)
    learned = {p: max(60, int(statistics.median(d) * 0.75)) for p, d in lives.items() if len(d) >= 2}
    graph._shelf = (graph.event_count, learned)
    return learned


def stale_days(f: Fact, subject_age: int | None = None, learned: dict | None = None) -> int | None:
    if f.stale_after:
        return f.stale_after
    if learned and f.p in learned:
        return learned[f.p]
    if f.p in STALE_AFTER:
        return STALE_AFTER[f.p]
    if f.p.startswith("current_"):
        return 120
    if subject_age is not None and subject_age < 18 and f.p in ("likes", "hobby", "interest", "favorite", "into"):
        return MINOR_INTEREST_STALE
    return None


def staleness(f: Fact, now: datetime, subject_age: int | None = None, graph: Graph | None = None) -> str | None:
    """'as of 2024-09, 2.0y ago' when a detail has outlived its shelf life, else None."""
    limit = stale_days(f, subject_age, shelf_lives(graph) if graph is not None else None)
    if limit is None or not f.last_t:
        return None
    age_days = (now - parse_time(f.last_t)).days
    if age_days < limit:
        return None
    ago = f"{age_days / 365.25:.1f}y" if age_days >= 365 else f"{age_days // 30}mo"
    return f"as of {f.last_t[:7]}, {ago} ago"


def build(graph: Graph, now: datetime) -> dict[str, Entity]:
    out: dict[str, Entity] = {}
    for f in graph.facts.values():
        if f.s == ME:
            continue
        e = out.get(f.s) or out.setdefault(f.s, Entity(id=f.s, name=f.s.replace("_", " ").title()))
        e.last_t = max(e.last_t, f.last_t)
        if not f.open:
            if f.p not in RESERVED_PREDICATES:
                e.past.append(f)
            continue
        if f.p == "name":
            e.name = f.o
        elif f.p == "kind":
            e.kind = f.o
        elif f.p == "relation_to_me":
            e.relations.append(slug(f.o))  # as said ("wife"); matching uses the canonical form
        elif f.p == "alias":
            e.aliases.append(f.o)
        else:
            e.facts.append(f)
        e.weight += f.score(now)
        if f.sens in ("private", "secret") and f.p in RESERVED_PREDICATES:
            e.sens = f.sens
    named: dict[str, list[Entity]] = defaultdict(list)
    for e in out.values():
        named[norm(e.name)].append(e)
        named[e.id].append(e)

    def owner_of(value: str, predicate: str) -> Entity | None:
        """The entity a fact's value refers to. Two people can share a name: prefer the one whose
        relationship matches, then one with no stated relationship."""
        cands = list({c.id: c for c in named.get(norm(value), [])}.values())
        if not cands:
            alias = by_alias(out, value)
            cands = [alias] if alias else []
        if len(cands) <= 1:
            return cands[0] if cands else None
        rel = canon_relation(predicate)
        return next((c for c in cands if rel in {canon_relation(r) for r in c.relations}),
                    next((c for c in cands if not c.relations), None))

    # "me.manager=Priya" says who Priya is to the user; a closed one says who she *was*.
    for f in sorted(graph.facts.values(), key=lambda f: f.open):
        if f.s != ME or f.p in RESERVED_PREDICATES:
            continue
        e = owner_of(f.o, f.p)
        if e is None:
            continue
        if f.open and slug(f.p) not in e.relations:
            e.relations.append(slug(f.p))
        elif not f.open and ent_rel(f.p):
            e.former.append(f)
    # Edges: a fact whose value names another entity links the two ("rebecca.best_friend=Maya").
    for f in graph.facts.values():
        if not f.open or f.p in RESERVED_PREDICATES and f.s != ME:
            continue
        target = owner_of(f.o, f.p)
        if target and target.id != f.s:
            target.links_in.append((f.s, f.p, f))
            if f.s in out:
                out[f.s].links_out.append((f.p, target.id, f))
    names = [(e, re.compile(r"\b" + re.escape(n) + r"\b", re.I))
             for e in out.values() for n in [e.name] + e.aliases if len(n) > 2]
    for n in graph.notes.values():
        if not n.open:
            continue
        if n.about and n.about in out:
            out[n.about].notes.append(n)
        for e, pattern in names:
            if e.id != n.about and pattern.search(n.text) and n not in e.mentions:
                e.mentions.append(n)
    for b in graph.beliefs.values():
        if b.about in out and b.open:
            out[b.about].beliefs.append(b)
    for e in out.values():
        e.relations = sorted(set(e.relations))
        e.aliases = sorted(set(e.aliases), key=str.lower)
    return out


def ent_rel(predicate: str) -> bool:
    return canon_relation(predicate) in RELATION_CANON


def by_alias(entities: dict[str, Entity], value: str) -> Entity | None:
    v = norm(value)
    for e in entities.values():
        if v in (norm(a) for a in e.aliases):
            return e
    return None


_POSSESSIVE = re.compile(r"^(my|our|the|your|his|her|their)\s+")


def resolve(entities: dict[str, Entity], mention: str, allow_id: bool = True) -> list[tuple[Entity, float, str]]:
    """Candidates for a mention like 'Rebecca', 'Becca', 'my sister' or 'my little sis'."""
    raw = mention.strip().strip("[]")
    if allow_id and raw in entities:  # an exact id, as printed in [brackets], is unambiguous
        return [(entities[raw], 1.0, "id")]
    m = _POSSESSIVE.sub("", norm(mention)).removesuffix("'s").strip()
    if not m:
        return []
    words = [slug(w) for w in m.split()]
    hits = []
    for e in entities.values():
        if m == norm(e.name):
            hits.append((e, 0.98, "name"))
        elif m in (norm(a) for a in e.aliases):
            hits.append((e, 0.95, "alias"))
        elif norm(e.name).split()[0] == m and len(m) > 2:
            hits.append((e, 0.9, "first name"))
        else:
            rels = {canon_relation(r) for r in e.relations}
            if {canon_relation(w) for w in words} & rels:
                hits.append((e, 0.85, "relation"))
            elif any(_WIDER.get(canon_relation(w), set()) & rels for w in words):
                hits.append((e, 0.6, "similar relation"))
    return sorted(hits, key=lambda h: (-h[1], -h[0].weight))


def mentioned_in(entities: dict[str, Entity], text: str) -> list[tuple[Entity, float, str]]:
    """Entities referred to anywhere in a free-text query ('gift ideas for my sister')."""
    if text.strip().strip("[]") in entities:
        return resolve(entities, text)
    tokens = norm(text).replace("'s", "").split()
    found: dict[str, tuple[Entity, float, str]] = {}
    for n in range(min(4, len(tokens)), 0, -1):
        for i in range(len(tokens) - n + 1):
            for e, score, why in resolve(entities, " ".join(tokens[i:i + n]), allow_id=False):
                if score > found.get(e.id, (None, 0.0))[1]:
                    found[e.id] = (e, score, why)
    return sorted(found.values(), key=lambda h: (-h[1], -h[0].weight))


def ambiguous(hits: list[tuple[Entity, float, str]]) -> bool:
    return len(hits) > 1 and hits[0][1] - hits[1][1] < 0.1


# ---- descriptive lookup ------------------------------------------------------------------
# "the kid I coach", "my friend from climbing", "the vet we use": no name, no alias. Resolved by
# searching each entity's whole profile, not just its label: its own facts, facts about it from
# others, notes that mention it, what models believe about it, and one hop through shared values
# (you coach "Hawks U12", Leo plays for "Hawks U12" -> Leo is connected to your coaching).

_DESCRIPTORS = {  # query word -> virtual profile token
    "kid": "minor", "kids": "minor", "child": "minor", "children": "minor", "teen": "minor",
    "teenager": "minor", "youngster": "minor", "junior": "minor", "baby": "minor", "toddler": "minor",
    "adult": "adult", "grownup": "adult", "elderly": "senior", "old": "senior", "older": "senior",
}


def _query_terms(text: str) -> list[str]:
    terms, prev = [], ""
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        possessed, prev = prev in ("my", "our"), w
        if possessed and w in RELATION_WORDS:  # "my kid" names a relationship, not an age group
            terms.append(canon_relation(w))
        elif w in _DESCRIPTORS:
            terms.append(_DESCRIPTORS[w])
        elif w in RELATION_WORDS:
            terms.append(canon_relation(w))
        else:
            terms += tokenize(w)
    return list(dict.fromkeys(terms))


def profile_parts(e: Entity, entities: dict[str, Entity], graph: Graph, now: datetime,
                  by_value: dict[str, list[Fact]]) -> list[tuple[str, int]]:
    """Human-readable evidence lines about an entity, each with a weight (repetitions in the index)."""
    name = e.name
    parts: list[tuple[str, int]] = [(f"{name} " + " ".join(e.aliases), 3)]
    if e.relations:
        extra = {canon_relation(r) for r in e.relations} - set(e.relations)
        parts.append(("your " + ", ".join(r.replace("_", " ") for r in e.relations)
                      + (f" ({', '.join(sorted(extra))})" if extra else ""), 2))
    if e.kind:
        parts.append((f"{name} is a {e.kind}", 1))
    age = e.age(now)
    if age:
        tag = "minor" if age[0] < 18 else "senior" if age[0] >= 65 else "adult"
        parts.append((f"{name} is {age[1].split(' (')[0]} years old ({tag})", 2))
    for f in e.facts:
        parts.append((f"{name} {f.p.replace('_', ' ')} {f.o}", 2))
        for other in by_value.get(norm(f.o), []):  # shared value: one hop
            if other.s != e.id and other.key != f.key:
                who = "you" if other.s == ME else entities[other.s].name if other.s in entities else other.s
                parts.append((f"{who}: {other.p.replace('_', ' ')} {other.o} (shared with {name})", 1))
    for src, p, f in e.links_in:
        who = "you" if src == ME else entities[src].name if src in entities else src
        parts.append((f"{who}: {p.replace('_', ' ')} {name}", 2))
    for p, dst, f in e.links_out:
        other = entities.get(dst)
        if other:
            parts.append((f"{name} {p.replace('_', ' ')} {other.name} (" + ", ".join(other.relations) + ")", 1))
    for n in e.notes + e.mentions:
        parts.append((f"note {n.t[:7]}: {n.text[:240]}", 1))
    for b in e.beliefs:
        parts.append((f"belief: {b.text}", 1))
    return parts


@dataclass
class Candidate:
    entity: Entity
    score: float
    coverage: float
    why: list[str]


def describe(entities: dict[str, Entity], graph: Graph, query: str, now: datetime,
             allowed=lambda e: True) -> tuple[list[Candidate], list[str]]:
    """Rank entities by how well their whole profile fits a description. Returns (candidates, terms)."""
    terms = _query_terms(query)
    if not terms or not entities:
        return [], terms
    by_value: dict[str, list[Fact]] = defaultdict(list)
    for f in graph.facts.values():
        if f.open and f.p not in RESERVED_PREDICATES:
            by_value[norm(f.o)].append(f)
    pool = [e for e in entities.values() if allowed(e)]
    parts = {e.id: profile_parts(e, entities, graph, now, by_value) for e in pool}
    docs = [(eid, " ".join(" ".join([text] * w) for text, w in ps)) for eid, ps in parts.items()]
    index = Index(docs)
    ranked = index.search(" ".join(terms))

    def has(eid, t):
        vocab = index.tfs[index.ids.index(eid)]
        return t in vocab or (len(t) >= 4 and any(v.startswith(t) for v in vocab))
    # Words nobody's profile contains carry no signal for choosing between people; leave them out of
    # the denominator. Rare words (striker) outweigh common ones (kid) via IDF.
    n = len(index.ids)
    weight = {t: math.log(1 + n / (1 + sum(has(i, t) for i in index.ids))) for t in terms}
    informative = [t for t in terms if any(has(i, t) for i in index.ids)]
    if not informative:
        return [], terms
    # Unknown words still count half against everyone: "the kid I coach who loves dinosaurs" is less
    # certain than "the kid I coach" when nothing on file mentions dinosaurs.
    total = sum(weight[t] for t in informative) + 0.5 * sum(weight[t] for t in terms if t not in informative)
    out = []
    for eid, score in ranked:
        e = entities[eid]
        credit, matched = 0.0, []
        for t in informative:
            if has(eid, t):
                credit += weight[t]
                matched.append(t)
            elif t in _AGE_TAGS and e.age(now) is None:
                credit += 0.5 * weight[t]  # age unknown: neither evidence for nor against
        why = [text for text, _ in parts[eid] if set(tokenize(text)) & set(matched)
               or any(t in text.lower() for t in matched)]
        out.append(Candidate(e, score, credit / total, list(dict.fromkeys(why))[:4]))
    out.sort(key=lambda c: (-c.coverage, -c.score))
    return out, terms


_AGE_TAGS = {"minor", "adult", "senior"}


def verdict(cands: list[Candidate]) -> str:
    """likely | possible | none: how sure we are that the top candidate is the one meant."""
    if not cands or cands[0].coverage < 0.5:
        return "none"
    top = cands[0]
    rival = cands[1] if len(cands) > 1 else None
    clear = rival is None or rival.coverage <= top.coverage - 0.15 or top.score >= 1.5 * rival.score
    return "likely" if top.coverage >= 0.8 and clear else "possible"


def mentions_of(graph: Graph, entities: dict[str, Entity], name: str) -> tuple[list[Note], list[Entity]]:
    """Notes that name someone who has no profile yet, and the known entities mentioned alongside."""
    pattern = re.compile(r"\b" + re.escape(name) + r"\b", re.I)
    notes = sorted((n for n in graph.notes.values() if n.open and pattern.search(n.text)), key=lambda n: n.t)
    near = [e for e in entities.values() if any(n in e.notes or n in e.mentions for n in notes)]
    return notes, near
