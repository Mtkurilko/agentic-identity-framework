"""Does the model know WHEN to consult memory? (needs the Anthropic SDK and credentials; costs money)

    pip install anthropic
    python3 evals/when_to_call.py --model claude-opus-5 --repeats 3

Two conditions, same questions, same store:
  baseline: a memory tool with a generic description, no capsule. This is how most memory tools ship.
  aif:      the capsule in the system prompt + the recall description carrying the live topic map.

Three kinds of question:
  archive:  the answer is on file but NOT in the capsule    -> should call recall
  capsule:  the answer is already in the capsule           -> should answer directly (aif only)
  generic:  nothing to do with the user                    -> should not call recall

Only the first assistant turn is scored (did it emit a `recall` tool_use?), which keeps each trial to one
request. Recall rate on `archive` is the headline; false-call rate on `generic` is the cost.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic  # noqa: E402

from aif.mcp import tool_defs  # noqa: E402
from aif.ops import Memory  # noqa: E402
from aif.store import Store  # noqa: E402

PROFILE = [  # (topic, predicate, value, importance[, extra]). High importance lands in the capsule.
    ("style", "response_style", "concise, code first, no filler", 3),
    ("food", "diet", "vegetarian", 3),
    ("places", "lives_in", "Lisbon", 3),
    ("tech", "editor", "neovim", 3),
    ("travel", "seat_pref", "aisle, never middle", 1),
    ("travel", "airline_avoid", "Ryanair", 1),
    ("travel", "loyalty_program", "TAP Miles&Go", 1),
    ("family", "sister", "Anna", 1),  # an introduction: Anna appears in the capsule's People roster
    ("family", "likes", "ceramics and Studio Ghibli", 1, {"subject": "Anna"}),
    ("family", "lives_in", "Porto", 1, {"subject": "Anna"}),
    ("family", "dog", "Miso", 1),
    ("family", "likes", "squeaky toys, hates baths", 1, {"subject": "Miso"}),
    ("hobbies", "hobby", "film photography on a Pentax K1000", 1),
    ("work", "manager", "Priya", 1),
    ("work", "standup_time", "09:30 Lisbon time", 1),
    ("media", "likes_books", "Ted Chiang", 1),
    ("fitness", "shoe_size", "EU 44", 1),
] + [("misc", "likes", f"filler item {i}", 2) for i in range(80)]  # pushes low-importance facts out

QUESTIONS = [
    ("archive", "Book me a flight to Berlin next Friday. What seat and airline should I pick?"),
    ("archive", "Suggest a birthday present for my sister."),
    ("archive", "What should I get my dog for Christmas? Something he'd actually like."),
    ("archive", "Draft a message to my manager saying I'll miss standup tomorrow."),
    ("archive", "I want new running shoes. Find me a good pair in my size."),
    ("archive", "Recommend a novel I'd probably love."),
    ("archive", "Plan a weekend photography outing for me with the gear I have."),
    ("capsule", "Suggest a dinner recipe for tonight."),
    ("capsule", "What's a good weekend day trip from where I live?"),
    ("capsule", "Write me a keybinding config for my editor to toggle line numbers."),
    ("generic", "What's the capital of Australia?"),
    ("generic", "Explain how a hash map handles collisions."),
    ("generic", "Convert 72 Fahrenheit to Celsius."),
    ("generic", "Write a haiku about autumn."),
]

BASELINE_TOOL = {
    "name": "recall",
    "description": "Search the user's saved memories.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
}
SYSTEM = "You are a helpful personal assistant."


def build_store() -> tuple[Memory, Path]:
    home = Path(tempfile.mkdtemp()) / "eval"
    store = Store(home, git=False)
    store.init(owner="Alex")
    mem = Memory(store, contributor="eval")
    for topic, pred, value, imp, *extra in PROFILE:
        mem.remember(topic, pred, value, "user_stated", evidence="(fixture)", importance=imp, force=True,
                     **(extra[0] if extra else {}))
    return mem, home.parent


def aif_tools(mem: Memory) -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["inputSchema"]}
            for t in tool_defs(mem) if t["name"] == "recall"]


def first_turn_calls_recall(client, model: str, system: str, tools: list[dict], question: str) -> bool:
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        tools=tools,
        messages=[{"role": "user", "content": question}],
    )
    if response.stop_reason == "refusal":
        return False
    return any(b.type == "tool_use" and b.name == "recall" for b in response.content)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--budget", type=int, default=420, help="capsule budget (small, so the archive matters)")
    args = ap.parse_args()

    mem, tmp = build_store()
    try:
        capsule = mem.capsule(budget=args.budget)
        conditions = {
            "baseline": (SYSTEM, [BASELINE_TOOL]),
            "aif": (SYSTEM + "\n\n" + capsule, aif_tools(mem)),
        }
        leaked = [v for _t, p, v, imp, *x in PROFILE if imp == 1 and p != "likes" and x and v in capsule]
        leaked += [p for _t, p, _v, imp, *x in PROFILE if imp == 1 and p != "likes" and not x
                   and p not in ("sister", "dog") and f"{p}=" in capsule]
        missing = [p for _t, p, _v, imp, *_ in PROFILE if imp == 3 and f"{p}=" not in capsule]
        if leaked or missing:
            sys.exit(f"fixture broken: archive facts in capsule {leaked}, capsule facts missing {missing}")
        client = anthropic.Anthropic()
        calls = defaultdict(lambda: [0, 0])  # (condition, kind) -> [called, total]
        for cond, (system, tools) in conditions.items():
            for kind, question in QUESTIONS:
                if cond == "baseline" and kind == "capsule":
                    continue
                for _ in range(args.repeats):
                    called = first_turn_calls_recall(client, args.model, system, tools, question)
                    calls[(cond, kind)][0] += called
                    calls[(cond, kind)][1] += 1
                    print(f"{cond:8} {kind:8} {'CALL' if called else '----'}  {question[:60]}", flush=True)
        print("\ncondition  question   recall-call rate  (want: archive high, capsule/generic low)")
        summary = {}
        for (cond, kind), (c, n) in sorted(calls.items()):
            print(f"{cond:9}  {kind:9}  {c}/{n} = {c / n:.0%}")
            summary[f"{cond}/{kind}"] = c / n
        print(json.dumps({"model": args.model, "capsule_tokens": mem.capsule(budget=args.budget, with_stats=True)[1]
                          ["tokens"], "rates": summary}, indent=2))
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
