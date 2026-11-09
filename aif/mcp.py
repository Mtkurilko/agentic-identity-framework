"""Minimal MCP server over stdio (JSON-RPC 2.0), standard library only.

Dual-era: clients that open with `initialize` get the session-based protocol (2025-11-25 and
earlier); clients that put the protocol version in each request's `_meta` (2026-07-28 and later)
are served statelessly, and `server/discover` answers their probe.

The server makes the model aware of the memory in three layers, from cheapest to most complete:
  1. `instructions` (injected by clients at session start) = the capsule: core, pinned facts,
     active facts, and the MAP of everything else on file.
  2. The `recall` tool's description embeds a live one-line topic map. Tool descriptions
     stay in context all session, so the model always knows what it could look up.
  3. `recall` itself returns compact lines with ids for follow-up writes.
"""
from __future__ import annotations

import json
import sys
import traceback

from aif import __version__
from aif.ops import Memory
from aif.render import map_inline, roster

MODERN_VERSIONS = ("2026-07-28",)
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # initialize-based
META = "io.modelcontextprotocol/"
LIST_TTL_MS = 5 * 60 * 1000  # the recall description carries a live map; let clients re-list every few minutes

REMEMBER_DESC = (
    "Save a durable fact about the user to their portable, permanent memory. Save things likely to matter "
    "again (preferences, constraints, biography, people, ongoing projects, how they like to work), not "
    "chit-chat or one-off task details. source=user_stated ONLY when the user said it: put their words in "
    "`evidence`. Your own guesses are source=inferred and stay tentative until the user confirms. Reuse "
    "existing topics/predicates from the map. Predicates are short snake_case (diet, lives_in, likes, "
    "seat_pref). If something changed (new job, moved, new school), set replace=true. Omit `predicate` "
    "to save a freeform note instead. PEOPLE, PETS, PLACES, THINGS in the user's life: set `subject` to their "
    "name (or 'my sister' / a known nickname, which resolves automatically). The first time, also set "
    "subject_relation (sister, manager, dog…) and any subject_aliases. Record time-sensitive details as they are "
    "now (grade=7, not 'next year'); the store dates everything and flags it when it gets old. If this returns "
    "CONFLICT, POSSIBLE DUPLICATE, AMBIGUOUS or UNKNOWN, nothing was saved: follow its instructions.")

REFLECT_DESC = (
    "Keep a working understanding of the user, and of the people in their life, that goes beyond stated facts: "
    "patterns, motives, values, unspoken preferences, context. `propose` a hypothesis when you notice something "
    "(it starts tentative). `support` or `contradict` an existing one [b…] with what you observed (`reason`). "
    "`revise` it when a better reading emerges; the old one is kept as history. Confidence moves with evidence "
    "from different models and conversations. Only the user's own words (`evidence`) can confirm or refute. "
    "Use this to anticipate what they need. Never present a hypothesis to the user as fact, and never let it "
    "override what they tell you.")

AMEND_DESC = (
    "Change the status of a stored item by its [id] (from recall or the capsule). confirm = the user just "
    "confirmed it (evidence = their words). retract = no longer true or wrong. pin/unpin = always include "
    "it in context (user's explicit request only). merge = the user says two people/things on file are the same: "
    "pass the duplicate as `id`, the one to keep as `into`, and their words as evidence. History is kept; nothing is "
    "deleted.")


def tool_defs(memory: Memory) -> list[dict]:
    topics = map_inline(memory.graph, memory.clearance)
    known = roster(memory.people(), memory.now(), memory.clearance)[:12]
    people = f" (known: {'; '.join(known)})" if known else ""
    return [
        {
            "name": "recall",
            "description": (
                "Look up the user's personal long-term memory. Call this BEFORE guessing any preference, "
                "personal detail, history, constraint or relationship of the user, whenever they mention someone "
                "or something in their life" + people + ", and whenever a request touches one of these topics "
                "(count: top predicates): " + topics + ". Query with keywords, a person or thing by name, "
                "relation or description ('my sister', 'Becca', 'the kid I coach'), 'me' for the user's full "
                "profile, a topic, or an [id]. Returns everything that could pertain, with LIKELY/POSSIBLE "
                "labels when it's inferring who you mean. If nothing is on file, ask the user."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords, a topic name, or an item id."},
                    "topic": {"type": "string", "description": "Restrict to one topic from the map."},
                    "history": {"type": "boolean", "description": "Include past/superseded facts."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "remember",
            "description": REMEMBER_DESC,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "e.g. food, work, travel, style, health, family"},
                    "predicate": {"type": "string", "description": "snake_case relation; omit for a freeform note"},
                    "value": {"type": "string", "description": "The value, or the note text"},
                    "source": {"type": "string", "enum": ["user_stated", "inferred"]},
                    "evidence": {"type": "string", "description": "The user's verbatim words (required for user_stated)"},
                    "subject": {"type": "string", "description": "Default 'me' (the user). Otherwise who/what the fact is about: a name, a known nickname, 'my sister', or an [id]"},
                    "subject_relation": {"type": "string", "description": "How they relate to the user: sister, mom, partner, manager, friend, dog, doctor…"},
                    "subject_kind": {"type": "string", "enum": ["person", "pet", "place", "org", "thing", "project", "event"]},
                    "subject_aliases": {"type": "array", "items": {"type": "string"}, "description": "Nicknames or other names"},
                    "stale_after": {"type": "integer", "description": "Days until this detail should be re-checked, if it changes on a schedule the store won't know (e.g. 365 for a school grade)"},
                    "single": {"type": "boolean", "description": "Predicate holds one value at a time"},
                    "replace": {"type": "boolean", "description": "User said the old value is no longer true"},
                    "importance": {"type": "integer", "enum": [1, 2, 3], "description": "1 trivia, 2 normal, 3 important"},
                    "since": {"type": "string", "description": "When it became true, e.g. 2019 or 2024-03"},
                    "sensitivity": {"type": "string", "enum": ["public", "personal", "private"]},
                    "pin": {"type": "boolean", "description": "Only if the user asked to always keep this in mind"},
                    "model": {"type": "string", "description": "Your model id, for provenance"},
                    "derived_from": {"type": "array", "items": {"type": "string"},
                                     "description": "Note ids this fact summarises (consolidation)"},
                    "force": {"type": "boolean", "description": "Save despite a possible-duplicate warning"},
                },
                "required": ["topic", "value", "source"],
            },
        },
        {
            "name": "reflect",
            "description": REFLECT_DESC,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["propose", "support", "contradict", "revise"]},
                    "text": {"type": "string", "description": "The hypothesis in one sentence (propose/revise)"},
                    "about": {"type": "string", "description": "Default the user; or a person/thing's name or [id]"},
                    "id": {"type": "string", "description": "Belief id [b…] (support/contradict/revise)"},
                    "reason": {"type": "string", "description": "What you observed that bears on it"},
                    "basis": {"type": "array", "items": {"type": "string"}, "description": "Fact/note ids it rests on"},
                    "evidence": {"type": "string", "description": "The user's own words, only if they confirmed/rejected it"},
                    "kind": {"type": "string", "enum": ["trait", "value", "preference", "pattern", "goal", "context",
                                                        "relationship"]},
                    "model": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        {
            "name": "amend",
            "description": AMEND_DESC,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["confirm", "retract", "pin", "unpin", "merge"]},
                    "into": {"type": "string", "description": "merge only: the person/thing to keep (name or [id])"},
                    "evidence": {"type": "string", "description": "The user's words"},
                    "reason": {"type": "string"},
                    "model": {"type": "string"},
                },
                "required": ["id", "action"],
            },
        },
    ]


SERVER_INFO = {"name": "aif", "title": "AIF personal memory", "version": __version__}


class Server:
    def __init__(self, memory: Memory, out=None):
        self.memory = memory
        self.out = out or sys.stdout
        self._topics = None
        self._base_contributor = memory.contributor
        self.modern = False

    def _cacheable(self) -> dict:
        return {"ttlMs": LIST_TTL_MS, "cacheScope": "private"} if self.modern else {}

    def send(self, msg: dict) -> None:
        self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.out.flush()

    def handle(self, msg: dict) -> dict | None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None or method.startswith("notifications/"):
            return None
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        version = meta.get(META + "protocolVersion")
        self.modern = method == "server/discover" or (version is not None and method != "initialize")
        if self.modern:
            if version is not None and version not in MODERN_VERSIONS:
                return {"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32022, "message": "Unsupported protocol version",
                    "data": {"supported": [*MODERN_VERSIONS, *SUPPORTED_VERSIONS], "requested": version}}}
            client = (meta.get(META + "clientInfo") or {}).get("name")
            self.memory.contributor = client if client and self._base_contributor == "unknown" \
                else self._base_contributor
        try:
            result = self.dispatch(method, params)
            if self.modern:
                result = {"resultType": "complete", **result,
                          "_meta": {**(result.get("_meta") or {}), META + "serverInfo": SERVER_INFO}}
        except KeyError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {e}"}}
        except Exception as e:  # never crash the client's session
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def dispatch(self, method: str, params: dict) -> dict:
        m = self.memory
        if method == "server/discover":
            return {
                "supportedVersions": [*MODERN_VERSIONS, *SUPPORTED_VERSIONS],
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "instructions": m.capsule(),
                "ttlMs": LIST_TTL_MS, "cacheScope": "private",
            }
        if method == "initialize":
            client = (params.get("clientInfo") or {}).get("name")
            if client and m.contributor == "unknown":
                m.contributor = client
            requested = params.get("protocolVersion")
            self._topics = self._topic_set()
            return {
                "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": True}, "resources": {}, "prompts": {}},
                "serverInfo": SERVER_INFO,
                "instructions": m.capsule(),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tool_defs(m), **self._cacheable()}
        if method == "tools/call":
            return self.call_tool(params.get("name"), params.get("arguments") or {})
        if method == "resources/list":
            return {"resources": [
                {"uri": "aif://capsule", "name": "capsule", "mimeType": "text/markdown",
                 "description": "Budgeted summary of the user's memory, for injection"},
                {"uri": "aif://review", "name": "review", "mimeType": "text/markdown",
                 "description": "Maintenance worksheet: renewals, unconfirmed inferences, consolidation"},
            ], **self._cacheable()}
        if method == "resources/read":
            uri = params.get("uri")
            text = {"aif://capsule": m.capsule, "aif://review": m.review}[uri]()
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}], **self._cacheable()}
        if method == "prompts/list":
            return {"prompts": [
                {"name": "about_me", "description": "Load my memory capsule into this conversation"},
                {"name": "memory_review", "description": "Walk me through renewing and tidying my memory"},
                {"name": "get_to_know_me", "description": "Short interview to fill in my memory and my AI's persona"},
            ], **self._cacheable()}
        if method == "prompts/get":
            name = params.get("name")
            text = {"about_me": m.capsule, "memory_review": m.review, "get_to_know_me": m.interview}[name]()
            return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        raise KeyError(method)

    def call_tool(self, name: str, args: dict) -> dict:
        m = self.memory
        try:
            if name == "recall":
                text = m.recall(args.get("query", ""), topic=args.get("topic"),
                                history=bool(args.get("history")), limit=int(args.get("limit", 12)))
            elif name == "remember":
                text = m.remember(
                    args["topic"], args.get("predicate"), args["value"], args["source"],
                    evidence=args.get("evidence"), subject=args.get("subject") or "me", single=args.get("single"),
                    replace=bool(args.get("replace")), importance=int(args.get("importance", 2)),
                    since=args.get("since"), sensitivity=args.get("sensitivity"), pin=bool(args.get("pin")),
                    model=args.get("model"), derived_from=args.get("derived_from"), force=bool(args.get("force")),
                    subject_kind=args.get("subject_kind"), subject_relation=args.get("subject_relation"),
                    subject_aliases=args.get("subject_aliases"), stale_after=args.get("stale_after"))
            elif name == "reflect":
                text = m.reflect(args["action"], text=args.get("text"), about=args.get("about"), id=args.get("id"),
                                 reason=args.get("reason"), basis=args.get("basis"), evidence=args.get("evidence"),
                                 kind=args.get("kind"), model=args.get("model"))
            elif name == "amend":
                text = m.amend(args["id"], args["action"], evidence=args.get("evidence"),
                               reason=args.get("reason"), model=args.get("model"), into=args.get("into"))
            else:
                return {"content": [{"type": "text", "text": f"Unknown tool {name!r}"}], "isError": True}
        except (KeyError, ValueError) as e:
            return {"content": [{"type": "text", "text": f"ERROR: bad arguments: {e}"}], "isError": True}
        if name != "recall" and not self.modern:
            topics = self._topic_set()
            if topics != self._topics:  # new topic or person: the recall description is stale
                self._topics = topics
                self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return {"content": [{"type": "text", "text": text}],
                "isError": text.startswith(("ERROR", "REFUSED"))}

    def _topic_set(self):
        m = self.memory
        return frozenset(m.graph.topic_map(m.clearance)) | frozenset(m.people())

    def serve(self, stream=None) -> None:
        for line in stream or sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            batch = msg if isinstance(msg, list) else [msg]
            replies = [r for r in (self.handle(x) for x in batch) if r is not None]
            for r in replies:
                self.send(r)
