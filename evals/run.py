"""Deterministic evaluation of AIF's core claims. No network, no API key.

    python3 evals/run.py            # writes evals/RESULTS.md

1. Format cost: tokens per fact for JSON, the pipe-table draft format, and the AIF capsule.
2. Flat prompt cost: simulate 10 years of one person's life; the capsule must stay within
   budget while a threshold-based selector (the "inject everything above 0.4" draft) grows.
3. Hallucination loop: a wrong guess repeated by models. Naive reinforcement vs provenance gating.
4. Conflict handling: scripted scenarios with expected outcomes.
"""
from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aif.events import fact_key  # noqa: E402
from aif.ops import Memory  # noqa: E402
from aif.render import estimate_tokens, topic_block  # noqa: E402
from aif.store import Store  # noqa: E402

MODELS = ["claude-opus-5-5", "claude-sonnet-5", "gpt-5", "gemini-3-pro", "llama-5", "local-qwen"]

# topic -> [(predicate, single-valued?, value pool)]
WORLD = {
    "food": [("diet", True, ["vegetarian", "pescatarian", "omnivore", "vegan"]),
             ("likes", False, ["thai curry", "ethiopian injera", "sourdough", "ramen", "tacos al pastor", "kimchi",
                               "dark chocolate", "espresso", "falafel", "pho", "dosa", "burrata"]),
             ("dislikes", False, ["cilantro", "olives", "blue cheese", "licorice", "okra"])],
    "work": [("employer", True, ["Nimbus Labs", "Acme Robotics", "Helio Systems", "self-employed", "Polar AI"]),
             ("role", True, ["ML engineer", "staff engineer", "research lead", "founder", "eng manager"]),
             ("project", False, [f"project {w}" for w in "atlas beacon cobalt delta ember falcon garnet harbor".split()]),
             ("tool", False, ["jupyter", "linear", "notion", "figma", "wandb", "grafana"])],
    "tech": [("editor", True, ["neovim", "vscode", "cursor", "zed", "helix"]),
             ("language", False, ["python", "rust", "typescript", "go", "zig", "elixir"]),
             ("primary_os", True, ["macOS", "linux", "NixOS"]),
             ("code_style", False, ["type hints everywhere", "small functions", "no ORMs", "tests first",
                                    "prefers stdlib"])],
    "style": [("response_style", False, ["concise", "low fluff", "technical when relevant", "show code first",
                                         "bullet points over prose", "no emojis"]),
              ("tone", False, ["dry humour", "direct", "warm but brief"])],
    "travel": [("seat_pref", True, ["aisle", "window"]),
               ("airline_avoid", False, ["Spirit", "Frontier", "Ryanair"]),
               ("visited", False, ["Lisbon", "Kyoto", "Mexico City", "Reykjavik", "Nairobi", "Hanoi", "Tbilisi",
                                   "Oaxaca", "Seoul", "Porto"])],
    "places": [("lives_in", True, ["Boston", "Denver", "Lisbon", "Austin", "Brooklyn"]),
               ("timezone", True, ["America/New_York", "America/Denver", "Europe/Lisbon", "America/Chicago"])],
    "family": [("partner", True, ["Sam", "Jordan", "Riley"]),
               ("sibling", False, ["Anna (sister)", "Ben (brother)"]),
               ("pet", False, ["dog named Miso", "cat named Tofu"])],
    "health": [("allergy", False, ["shellfish", "penicillin"]),
               ("exercise", False, ["runs 3x/week", "climbing", "yoga", "cycling to work"])],
    "media": [("likes_music", False, ["Radiohead", "The Beatles", "Aphex Twin", "Bon Iver", "Fela Kuti",
                                      "Nils Frahm", "Khruangbin"]),
              ("likes_books", False, ["Ted Chiang", "Ursula K. Le Guin", "Borges", "Octavia Butler"]),
              ("likes_film", False, ["Tarkovsky", "Studio Ghibli", "Wong Kar-wai"])],
    "goals": [("goal", False, ["run a marathon", "learn Portuguese", "ship an open-source project",
                               "write a book", "save for a house", "learn piano"])],
    "money": [("budget_style", False, ["frugal on gadgets", "splurges on travel", "index funds only"])],
    "hobbies": [("hobby", False, ["film photography", "bouldering", "sourdough baking", "chess", "synths",
                                  "woodworking", "birding"])],
}


HYPOTHESES = ["works best with blunt, direct feedback", "procrastinates on admin tasks, not on building",
              "values autonomy over structure", "gets energised by teaching others",
              "prefers async communication over meetings", "is saving toward a big purchase",
              "tends to over-commit in spring", "reads more fiction when stressed"]


def simulate(years: int = 10, weeks_per_year: int = 52, seed: int = 7):
    rng = random.Random(seed)
    home = Path(tempfile.mkdtemp()) / "sim"
    store = Store(home, git=False)
    store.init(owner="Alex")
    store.core_path.write_text("Alex. Builder, runner, cook. Wants blunt technical answers and no hedging.\n")
    clock = [datetime(2026, 1, 5, tzinfo=timezone.utc)]
    mem = Memory(store, contributor="sim", clock=lambda: clock[0])
    mem.remember("health", "allergy", "shellfish", "user_stated", evidence="I'm allergic to shellfish", pin=True,
                 importance=3)
    per_year, notes_written, burst = [], 0, {}
    for year in range(years):
        for week in range(weeks_per_year):
            clock[0] += timedelta(days=7)
            for _ in range(rng.randint(3, 8)):  # interactions this week
                topic = rng.choice(list(WORLD))
                pred, single, pool = rng.choice(WORLD[topic])
                value = rng.choice(pool)
                if pred in ("likes", "visited", "project", "goal", "hobby"):  # an open-ended long tail
                    value = f"{value} #{rng.randint(1, 40 + year * 25)}"
                roll = rng.random()
                if roll < 0.06:  # a model reflects: proposes a hypothesis or weighs in on one
                    open_b = [b for b in mem.graph.beliefs.values() if b.status == "open"]
                    if open_b and rng.random() < 0.7:
                        b = rng.choice(open_b)
                        mem.reflect(rng.choice(["support", "support", "contradict"]), id=b.id,
                                    reason=f"observed in week {week}", model=rng.choice(MODELS))
                    else:
                        mem.reflect("propose", text=rng.choice(HYPOTHESES), model=rng.choice(MODELS),
                                    reason="pattern across sessions")
                    continue
                if roll < 0.55:
                    mem.remember(topic, pred, value, "user_stated", evidence=f"(week {week}) {value}",
                                 single=single, replace=single, importance=rng.choice([1, 2, 2, 3]), force=True)
                elif roll < 0.85:
                    mem.remember(topic, pred, value, "inferred", model=rng.choice(MODELS), single=single,
                                 force=True)
                else:
                    mem.note(topic, f"episode: {pred} {value}, week {week} of year {year + 1}", "user_stated",
                             evidence="said in passing")
                    notes_written += 1
            if year == 5 and week == 20:
                # Onboarding burst: the user pastes a structured profile from another assistant.
                for i in range(150):
                    topic = rng.choice(list(WORLD))
                    pred, _single, pool = rng.choice(WORLD[topic])
                    mem.remember(topic, pred, f"{rng.choice(pool)} (imported {i})", "user_stated",
                                 evidence="(profile import)", single=False, force=True)
                _, bstats = mem.capsule(with_stats=True)
                live = [f for f in mem.graph.facts.values() if f.open]
                burst = {"capsule_tokens": bstats["tokens"], "threshold_tokens": bstats["fixed_tokens"] +
                         estimate_tokens(topic_block([f for f in live if f.score(clock[0]) > 0.4 or f.pinned]))}
        t0 = time.perf_counter()
        mem._graph = None
        g = mem.graph
        replay_ms = (time.perf_counter() - t0) * 1000
        text, stats = mem.capsule(with_stats=True)
        t0 = time.perf_counter()
        for q in ("diet", "where do I live", "favourite music", "seat", "allergy", "editor"):
            mem.recall(q)
        recall_ms = (time.perf_counter() - t0) * 1000 / 6
        open_facts = [f for f in g.facts.values() if f.open]
        threshold = [f for f in open_facts if f.score(clock[0]) > 0.4 or f.pinned]
        per_year.append({
            "year": year + 1, "events": g.event_count, "open_facts": len(open_facts),
            "all_facts": len(g.facts), "notes": notes_written,
            "capsule_tokens": stats["tokens"], "budget": stats["budget"],
            "threshold_tokens": estimate_tokens(topic_block(threshold)) + stats["fixed_tokens"],
            "full_dump_tokens": estimate_tokens(topic_block(open_facts)),
            "disk_kb": sum(p.stat().st_size for p in store.event_files()) // 1024,
            "entities": stats["entities"], "beliefs": sum(1 for b in g.beliefs.values() if b.status == "open"),
            "replay_ms": round(replay_ms, 1), "recall_ms": round(recall_ms, 2),
        })
    capsule_text = mem.capsule()
    shutil.rmtree(home.parent)
    return per_year, capsule_text, burst


def format_cost():
    rng = random.Random(1)
    facts = []
    for topic, preds in WORLD.items():
        for pred, _single, pool in preds:
            for value in rng.sample(pool, min(2, len(pool))):
                facts.append((topic, pred, value))
    as_json = json.dumps([{"subject": "Alex", "predicate": p, "object": o, "topic": t, "confidence": 0.9,
                           "salience": 0.8, "source": "user_stated", "since": "2026-02", "until": None}
                          for t, p, o in facts], indent=1)
    as_pipe = "\n".join(f"Alex|{p}|{o}|0.90|0.80|U|2026-02|-" for _t, p, o in facts)

    class F:  # minimal stand-in for topic_block
        def __init__(self, t, p, o):
            self.topic, self.p, self.o, self.s, self.tentative, self.key = t, p, o, "me", False, p + o
    as_aif = topic_block([F(*f) for f in facts])
    n = len(facts)
    return n, {name: (estimate_tokens(text), estimate_tokens(text) / n)
               for name, text in (("JSON records", as_json), ("pipe table (draft v1)", as_pipe),
                                  ("AIF capsule block", as_aif))}


def hallucination_loop(rounds: int = 12):
    """A wrong guess ('night owl') is re-inferred each session. Naive: every mention adds weight."""
    naive, rows = 0.5, []
    home = Path(tempfile.mkdtemp()) / "h"
    store = Store(home, git=False)
    store.init()
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    mem = Memory(store, clock=lambda: clock[0])
    key = fact_key("me", "chronotype", "night owl")
    for i in range(rounds):
        clock[0] += timedelta(days=3)
        model = "claude-opus-5-5" if i < 8 else MODELS[i % len(MODELS)]  # same model, then others
        mem.remember("habits", "chronotype", "night owl", "inferred", model=model)
        naive = min(1.0, naive + 0.1)
        f = mem.graph.facts[key]
        rows.append((i + 1, model, naive, f.conf, "established" if not f.tentative else "tentative"))
    shutil.rmtree(home.parent)
    return rows


SCENARIOS = [
    # (description, setup calls, final call, expected prefix)
    ("new job stated after old one, no replace flag", [("work", "employer", "Nimbus", "U", {})],
     ("work", "employer", "Acme", "U", {}), "CONFLICT"),
    ("new job stated with replace", [("work", "employer", "Nimbus", "U", {})],
     ("work", "employer", "Acme", "U", {"replace": True}), "saved"),
    ("model guesses a different employer", [("work", "employer", "Nimbus", "U", {})],
     ("work", "employer", "Acme", "I", {"replace": True}), "REFUSED"),
    ("second liked band is not a conflict", [("media", "likes_music", "Radiohead", "U", {})],
     ("media", "likes_music", "Bon Iver", "U", {}), "saved"),
    ("paraphrase of an existing like", [("media", "likes_music", "The Beatles", "U", {})],
     ("media", "likes_music", "beatles, the band", "U", {}), "POSSIBLE DUPLICATE"),
    ("user corrects a model's tentative guess", [("places", "lives_in", "Denver", "I", {})],
     ("places", "lives_in", "Boston", "U", {}), "saved"),
    ("user_stated without evidence", [], ("food", "diet", "vegan", "U_noev", {}), "saved"),
    ("same fact re-stated", [("food", "diet", "vegan", "U", {})], ("food", "diet", "vegan", "U", {}), "reinforced"),
]


def conflicts():
    results = []
    for desc, setup, final, expected in SCENARIOS:
        home = Path(tempfile.mkdtemp()) / "c"
        store = Store(home, git=False)
        store.init()
        mem = Memory(store)

        def call(topic, pred, value, src, kw):
            if src == "U":
                return mem.remember(topic, pred, value, "user_stated", evidence="(said)", **kw)
            if src == "U_noev":
                return mem.remember(topic, pred, value, "user_stated", **kw)
            return mem.remember(topic, pred, value, "inferred", model="m", **kw)
        for s in setup:
            call(*s)
        out = call(*final)
        results.append((desc, expected, out.split(" ", 1)[0].rstrip(",.:"), out.startswith(expected)))
        shutil.rmtree(home.parent)
    return results


def people_over_time():
    """Cross-model, cross-year story: ChatGPT learns about Rebecca in 2024; Claude is asked in 2026."""
    home = Path(tempfile.mkdtemp()) / "p"
    store = Store(home, git=False)
    store.init(owner="Alex")
    clock = [datetime(2024, 9, 10, tzinfo=timezone.utc)]
    gpt = Memory(store, contributor="chatgpt", clock=lambda: clock[0])
    said = dict(source="user_stated", evidence="(said)")
    gpt.remember("family", "sister", "Rebecca", **said)
    gpt.remember("family", "school", "Lincoln Middle School", subject="my sister", subject_aliases=["Becca"], **said)
    gpt.remember("family", "age", "11", subject="Becca", **said)
    gpt.remember("family", "likes", "horses", subject="Rebecca", **said)
    clock[0] = datetime(2026, 9, 25, tzinfo=timezone.utc)
    claude = Memory(store, contributor="claude", clock=lambda: clock[0])
    checks = []

    def check(desc, text, *needles):
        checks.append((desc, all(n in text for n in needles)))
    gift = claude.recall("what should I get my sister for her birthday")
    check("'my sister' (via Claude) finds Rebecca (recorded via ChatGPT)", gift, "Rebecca", "your sister")
    check("age projected forward: 11 in 2024-09 → ≈13 now", gift, "age ≈13")
    check("2-year-old school detail flagged as possibly outdated", gift, "school=Lincoln Middle School (as of 2024-09")
    check("a child's 2-year-old interests flagged", gift, "likes=horses (as of 2024-09")
    check("told to ask only if it matters", gift, "If it doesn't matter, don't ask")
    check("nickname 'Becca' resolves", claude.recall("Becca"), "Rebecca", "Lincoln")
    check("'my brother' (unknown) → ask for a name, don't invent", claude.recall("gift for my brother"),
          "No brother on file")
    claude.remember("family", "sister", "Anna", **said)
    check("second sister → 'my sister' is ambiguous, one question", claude.recall("my sister"),
          "AMBIGUOUS", "Anna (sister) or Rebecca (sister)?")
    out = claude.remember("work", "team", "platform", subject="Rebecca", subject_relation="coworker", **said)
    check("coworker also named Rebecca → separate person", out, "rebecca_coworker", "kept separate")
    claude.remember("family", "school", "Roosevelt High", subject="rebecca", replace=True, **said)
    after = claude.recall("rebecca")
    check("update keeps history: Lincoln → Roosevelt", after, "school=Roosevelt High\n",
          "earlier: school=Lincoln Middle School")
    check("both models credited", after, "chatgpt", "claude")
    shutil.rmtree(home.parent)
    return checks, gift


def lookup_and_worldview():
    """Descriptive lookup ('the kid I coach') and a hypothesis evolving across models and months."""
    home = Path(tempfile.mkdtemp()) / "w"
    store = Store(home, git=False)
    store.init(owner="Alex")
    clock = [datetime(2026, 3, 1, tzinfo=timezone.utc)]
    mem = Memory(store, contributor="claude", clock=lambda: clock[0])
    said = dict(source="user_stated", evidence="(said)")
    mem.remember("hobbies", "coaches", "Hawks U12 soccer", **said)
    mem.remember("family", "sister", "Rebecca", **said)
    mem.remember("family", "age", "13", subject="Rebecca", **said)
    for name, pos, age in (("Leo", "goalkeeper", "11"), ("Sam", "striker", None)):
        mem.remember("sports", "plays_for", "Hawks U12 soccer", subject=name, subject_relation="player", **said)
        mem.remember("sports", "position", pos, subject=name, **said)
        if age:
            mem.remember("sports", "age", age, subject=name, **said)
    mem.note("sports", "Leo made a great save; his dad Marco drove carpool", "user_stated", evidence="x")
    mem.remember("family", "partner", "Sam Rivera", **said)
    checks = []

    def check(desc, text, *needles, starts=None):
        ok = all(n in text for n in needles) and (starts is None or text.startswith(starts))
        checks.append((desc, ok))
    check("'the kid I coach' → Leo, via a shared value (you coach the team he plays for) and his age",
          mem.recall("the kid I coach"), "shared with Leo", starts="LIKELY: 'the kid I coach' is probably Leo")
    check("'the kid who plays striker' → Sam: the rare word outweighs the common one",
          mem.recall("the kid who plays striker"), starts="LIKELY: 'the kid who plays striker' is probably Sam")
    check("an unmatched detail ('…who loves dinosaurs') lowers certainty to POSSIBLE",
          mem.recall("the kid I coach who loves dinosaurs"), starts="POSSIBLE")
    check("unknown name 'Marco' → where he's mentioned, not a guess", mem.recall("Marco"),
          "no profile yet", "mentioned alongside: Leo")
    check("'my kid's dentist' → no child on file; ask", mem.recall("my kid's dentist"), starts="No child on file")
    check("writing about 'the goalkeeper I coach' asks before saving",
          mem.remember("sports", "needs", "gloves", subject="the goalkeeper I coach", **said), "subject='leo'",
          starts="UNSURE")
    clock[0] = datetime(2027, 5, 1, tzinfo=timezone.utc)
    mem.remember("family", "partner", "Jordan", replace=True, **said)
    check("partner changes: Sam's file says 'formerly your partner'", mem.recall("Sam Rivera"),
          "formerly: your partner (2026-03→2027-05)")

    b = mem.reflect("propose", text="Alex's identity is bound up in mentoring kids", model="claude",
                    reason="talks about the team constantly").split("]")[0].split("[")[1]
    trail = [("claude proposes", mem.graph.beliefs[b].conf)]
    for action, model, why in (("support", "gpt-5", "volunteered extra practices"),
                               ("support", "claude", "spent 2h planning drills"),
                               ("contradict", "gemini-3-pro", "said coaching is draining, may quit")):
        mem.reflect(action, id=b, reason=why, model=model)
        trail.append((f"{model} {action}s: {why}", mem.graph.beliefs[b].conf))
    new = mem.reflect("revise", id=b, text="Alex cares about the kids but coaching is costing energy; may be "
                                           "the last season", model="gemini-3-pro").split("→ [")[1].split("]")[0]
    trail.append(("gemini-3-pro revises the reading", mem.graph.beliefs[new].conf))
    mem.amend(new, "confirm", evidence="yeah, probably my last season")
    trail.append(("user confirms in their own words", mem.graph.beliefs[new].conf))
    check("revised belief keeps its lineage", mem.recall(new), "revised from: [" + b)
    check("confirmed understanding appears in the capsule", mem.capsule(), "(confirmed by user)")
    shutil.rmtree(home.parent)
    return checks, trail


def main():
    n, costs = format_cost()
    years, final_capsule, burst = simulate()
    loop = hallucination_loop()
    conf = conflicts()
    people_checks, gift_output = people_over_time()
    lookup_checks, trail = lookup_and_worldview()
    tok = "tiktoken o200k" if __import__("aif.render", fromlist=["_ENC"])._ENC else "heuristic estimator (±15%)"

    out = ["# AIF evaluation results", "",
           f"_Generated by `python3 evals/run.py`, deterministic (seeded). Token counts: {tok}._", "",
           f"## 1. Format cost ({n} identical facts)", "", "| format | tokens | tokens/fact |", "|---|---:|---:|"]
    out += [f"| {k} | {v[0]} | {v[1]:.1f} |" for k, v in costs.items()]
    base = costs["JSON records"][0]
    out += ["", f"The capsule block uses {costs['AIF capsule block'][0] / base:.0%} of the JSON tokens and "
            f"{costs['AIF capsule block'][0] / costs['pipe table (draft v1)'][0]:.0%} of the pipe table's. "
            "Grouping by topic removes the repeated subject, and per-row metadata moves to recall.", ""]

    out += ["## 2. Ten simulated years", "",
            "~5 interactions/week: stated facts, model inferences from 6 different models, job/city changes, "
            "episodic notes, an open-ended long tail of likes/places/projects, and a 150-fact profile import "
            "in year 6.", "",
            "| year | events | current facts | people | beliefs | notes | **capsule tokens** | threshold>0.4 tokens | "
            "dump-all tokens | log KB | replay ms | recall ms |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    out += [f"| {y['year']} | {y['events']} | {y['open_facts']} | {y['entities']} | {y['beliefs']} | {y['notes']} | "
            f"**{y['capsule_tokens']}**/"
            f"{y['budget']} | {y['threshold_tokens']} | {y['full_dump_tokens']} | {y['disk_kb']} | "
            f"{y['replay_ms']} | {y['recall_ms']} |" for y in years]
    first, last = years[0], years[-1]
    out += ["", f"Over 10 years the log grew {last['events'] / first['events']:.1f}×. The capsule never exceeded "
            f"{max(y['capsule_tokens'] for y in years)} tokens. Dumping every current fact would cost "
            f"{last['full_dump_tokens']} tokens by year 10. Salience decay alone keeps the threshold rule near "
            f"budget in quiet years. It has no bound, though: in year 6 the user imports a 150-fact profile in "
            f"one week. Measured right after, the threshold rule injects {burst['threshold_tokens']} tokens "
            f"while the capsule stays at {burst['capsule_tokens']}.", "",
            "<details><summary>Capsule after 10 simulated years</summary>", "", "```", final_capsule.strip(),
            "```", "</details>", ""]

    out += ["## 3. Hallucination feedback loop", "",
            "A wrong inference (`chronotype=night owl`) is re-asserted every session. Rounds 1–8 come from the "
            "same model; rounds 9–12 come from other models. The naive model adds 0.1 weight per mention.", "",
            "| round | contributor | naive weight | AIF confidence | AIF status |", "|---:|---|---:|---:|---|"]
    out += [f"| {r} | {m} | {nv:.2f} | {c:.2f} | {s} |" for r, m, nv, c, s in loop]
    out += ["", "Repetition by one model adds nothing. Independent models raise confidence, capped at 0.79. "
            "Only the user's own words can establish the fact.", ""]

    passed = sum(1 for r in conf if r[3])
    out += [f"## 4. Conflict & provenance scenarios: {passed}/{len(conf)} as expected", "",
            "| scenario | expected | got | ok |", "|---|---|---|---|"]
    out += [f"| {d} | {e} | {g} | {'✅' if ok else '❌'} |" for d, e, g, ok in conf]
    out.append("")

    ok_people = sum(1 for _, ok in people_checks if ok)
    out += [f"## 5. People over time: {ok_people}/{len(people_checks)} as expected", "",
            "ChatGPT records the user's sister Rebecca (11, Lincoln Middle School, likes horses) in Sep 2024. "
            "Two years later, a different model is asked for a birthday gift for \"my sister\".", "",
            "| check | ok |", "|---|---|"]
    out += [f"| {d} | {'✅' if ok else '❌'} |" for d, ok in people_checks]
    out += ["", "What the second model gets back from `recall(\"what should I get my sister for her birthday\")`:",
            "", "```", gift_output, "```", ""]
    ok_lookup = sum(1 for _, ok in lookup_checks if ok)
    out += [f"## 6. Descriptive lookup and a world-view that evolves: {ok_lookup}/{len(lookup_checks)} as expected",
            "", "| check | ok |", "|---|---|"]
    out += [f"| {d} | {'✅' if ok else '❌'} |" for d, ok in lookup_checks]
    out += ["", "One hypothesis over fourteen months (confidence after each step):", "",
            "| step | confidence |", "|---|---:|"]
    out += [f"| {step} | {c:.2f} |" for step, c in trail]
    out.append("")
    path = Path(__file__).with_name("RESULTS.md")
    path.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    return 0 if passed == len(conf) and ok_people == len(people_checks) and ok_lookup == len(lookup_checks) and all(y["capsule_tokens"] <= y["budget"] for y in years) else 1


if __name__ == "__main__":
    raise SystemExit(main())
