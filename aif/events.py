"""Event schema — the only ground truth in an AIF store.

Every line of events/*.jsonl is one event. Events are content-addressed: `id` is a
hash of the event's own body, so an edited line no longer matches its id (tamper
evidence that survives outside git), identical events from two devices collapse to
one on merge, and replay order never depends on file order.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

SCHEMA = 1
OPS = ("assert", "retract", "confirm", "pin", "unpin", "note", "define", "belief", "evidence", "merge")
SOURCES = {"U": "user_stated", "I": "inferred", "X": "imported"}
SOURCE_ALIASES = {"u": "U", "user": "U", "user_stated": "U", "stated": "U",
                  "i": "I", "inferred": "I", "model_inferred": "I", "guess": "I",
                  "x": "X", "imported": "X", "import": "X"}
SENSITIVITY = ("public", "personal", "private", "secret")
ME = "me"

REQUIRED = {
    "assert": ("s", "p", "o", "topic", "src", "by", "conf"),
    "retract": ("ref",),
    "confirm": ("ref", "src", "by"),
    "pin": ("ref", "src"),
    "unpin": ("ref",),
    "note": ("text", "topic", "src", "by"),
    "define": ("p",),
    "belief": ("about", "text", "src", "by"),
    "evidence": ("ref", "dir", "src", "by"),
    "merge": ("from", "into", "src", "by"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(t: str) -> datetime:
    """Parse the ISO-ish timestamps AIF writes (and the coarser ones humans write)."""
    t = t.strip().replace("Z", "+00:00")
    for fmt_len, pad in ((4, "-01-01"), (7, "-01")):
        if len(t) == fmt_len:
            t += pad
    dt = datetime.fromisoformat(t)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def canonical(ev: dict) -> str:
    body = {k: v for k, v in ev.items() if k != "id"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_id(ev: dict) -> str:
    return hashlib.sha256(canonical(ev).encode("utf-8")).hexdigest()[:16]


def seal(ev: dict) -> dict:
    """Return a copy of `ev` with schema version and content-addressed id filled in."""
    ev = {k: v for k, v in ev.items() if v is not None and k != "id"}
    ev.setdefault("v", SCHEMA)
    ev["id"] = event_id(ev)
    return ev


_WS = re.compile(r"\s+")


def norm(text) -> str:
    """Normalise a value for identity comparison (case/spacing/trailing punctuation)."""
    return _WS.sub(" ", str(text).strip().lower()).strip(" .,!;:")


def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_") or "misc"


def fact_key(s: str, p: str, o: str) -> str:
    raw = f"{norm(s)}|{slug(p)}|{norm(o)}"
    return "k" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:9]


def note_id(ev: dict) -> str:
    return "n" + ev["id"][:9]


def belief_id(ev: dict) -> str:
    return "b" + ev["id"][:9]


def source_code(value: str) -> str:
    code = SOURCE_ALIASES.get(str(value).strip().lower())
    if code is None:
        raise ValueError(f"unknown source {value!r}; use user_stated, inferred or imported")
    return code


def validate(ev: dict) -> list[str]:
    """Problems with one event. An empty list means the event is well-formed."""
    problems = []
    op = ev.get("op")
    if op not in REQUIRED:
        # Unknown ops are legal (forward compatibility) but reported.
        return [f"unknown op {op!r} (ignored by this reader)"]
    for key in ("t",) + REQUIRED[op]:
        if key not in ev or ev[key] in ("", None):
            problems.append(f"{op}: missing {key!r}")
    if "src" in ev and ev["src"] not in SOURCES:
        problems.append(f"bad src {ev['src']!r}")
    if op == "evidence" and ev.get("dir") not in ("+", "-"):
        problems.append(f"bad dir {ev.get('dir')!r}")
    if "sens" in ev and ev["sens"] not in SENSITIVITY:
        problems.append(f"bad sens {ev['sens']!r}")
    if "conf" in ev and not (isinstance(ev["conf"], (int, float)) and 0 <= ev["conf"] <= 1):
        problems.append(f"bad conf {ev['conf']!r}")
    if "id" in ev and ev["id"] != event_id(ev):
        problems.append("id does not match content (line was edited after it was written)")
    return problems
