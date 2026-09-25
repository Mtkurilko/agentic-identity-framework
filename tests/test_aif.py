import io
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aif.events import event_id, fact_key, seal, validate
from aif.graph import INFERRED_CEILING, Graph
from aif.mcp import Server
from aif.ops import Memory
from aif.render import estimate_tokens
from aif.store import Store

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, days):
        self.t += timedelta(days=days)


class Base(unittest.TestCase):
    git = False

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir / "me", git=self.git)
        self.store.init(owner="Alex")
        self.clock = Clock()
        self.mem = self.memory()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def memory(self, clearance="personal", contributor="test"):
        return Memory(self.store, clearance=clearance, contributor=contributor, clock=self.clock)

    def said(self, topic, p, o, words="(user said so)", **kw):
        return self.mem.remember(topic, p, o, "user_stated", evidence=words, **kw)

    def guessed(self, topic, p, o, model, **kw):
        return self.mem.remember(topic, p, o, "inferred", model=model, **kw)

    def fact(self, p, o, s="me"):
        return self.mem.graph.facts[fact_key(s, p, o)]


class EventTests(unittest.TestCase):
    def test_ids_are_content_addressed_and_detect_edits(self):
        ev = seal({"t": "2026-01-01T00:00:00Z", "op": "assert", "s": "me", "p": "diet", "o": "vegan",
                   "topic": "food", "src": "U", "by": "x", "conf": 0.9})
        self.assertEqual(ev["id"], event_id(ev))
        self.assertEqual(validate(ev), [])
        tampered = dict(ev, o="carnivore")
        self.assertTrue(any("edited" in p for p in validate(tampered)))

    def test_replay_is_order_independent_and_dedups(self):
        a = seal({"t": "2026-01-01T00:00:00Z", "op": "assert", "s": "me", "p": "likes", "o": "tea",
                  "topic": "food", "src": "U", "by": "x", "conf": 0.9})
        b = seal({"t": "2026-01-02T00:00:00Z", "op": "retract", "ref": fact_key("me", "likes", "tea")})
        g1, g2 = Graph.replay([a, b]), Graph.replay([b, a, a])  # union merge from two devices
        self.assertFalse(g1.facts[fact_key("me", "likes", "tea")].open)
        self.assertFalse(g2.facts[fact_key("me", "likes", "tea")].open)
        self.assertEqual(g2.event_count, 2)

    def test_unknown_ops_are_tolerated(self):
        g = Graph.replay([{"v": 9, "t": "2030-01-01T00:00:00Z", "op": "teleport", "id": "x"}])
        self.assertEqual(g.event_count, 0)
        self.assertTrue(g.problems)


class ProvenanceTests(Base):
    def test_user_stated_without_evidence_is_downgraded(self):
        out = self.mem.remember("food", "diet", "vegan", "user_stated")
        self.assertIn("saved as inferred", out)
        self.assertEqual(self.fact("diet", "vegan").src, "I")

    def test_one_model_repeating_itself_does_not_compound(self):
        for _ in range(10):
            self.guessed("work", "prefers", "async communication", "model-a")
        self.assertLessEqual(self.fact("prefers", "async communication").conf, 0.6)

    def test_independent_models_raise_confidence_but_never_establish(self):
        for model in ("model-a", "model-b", "model-c", "model-d"):
            self.guessed("work", "prefers", "async communication", model)
        f = self.fact("prefers", "async communication")
        self.assertGreater(f.conf, 0.6)
        self.assertLessEqual(f.conf, INFERRED_CEILING)
        self.assertTrue(f.tentative)

    def test_user_confirmation_establishes_an_inference(self):
        self.guessed("work", "prefers", "async communication", "model-a")
        key = fact_key("me", "prefers", "async communication")
        self.assertIn("REFUSED", self.mem.amend(key, "confirm"))
        self.mem.amend(key, "confirm", evidence="yes, async please")
        f = self.fact("prefers", "async communication")
        self.assertFalse(f.tentative)
        self.assertEqual(f.src, "U")

    def test_models_cannot_pin(self):
        out = self.guessed("health", "allergy", "peanuts", "model-a", pin=True)
        self.assertIn("pin ignored", out)
        self.assertFalse(self.fact("allergy", "peanuts").pinned)

    def test_models_cannot_retract_user_facts_without_user_words(self):
        self.said("food", "diet", "vegetarian")
        key = fact_key("me", "diet", "vegetarian")
        self.assertIn("REFUSED", self.mem.amend(key, "retract", reason="seemed to eat fish"))
        self.assertTrue(self.fact("diet", "vegetarian").open)


class ConflictTests(Base):
    def test_single_valued_conflict_asks_instead_of_writing(self):
        self.said("work", "employer", "Nimbus Labs")
        out = self.said("work", "employer", "Acme")
        self.assertTrue(out.startswith("CONFLICT"))
        self.assertNotIn(fact_key("me", "employer", "Acme"), self.mem.graph.facts)

    def test_replace_closes_old_and_keeps_history(self):
        self.said("work", "employer", "Nimbus Labs")
        self.said("work", "employer", "Acme", replace=True)
        old, new = self.fact("employer", "Nimbus Labs"), self.fact("employer", "Acme")
        self.assertFalse(old.open)
        self.assertEqual(old.closed_by, new.key)
        self.assertIn("Nimbus Labs", self.mem.recall("employer", history=True))

    def test_inferences_cannot_replace_stated_facts(self):
        self.said("work", "employer", "Nimbus Labs")
        out = self.guessed("work", "employer", "Acme", "model-a", replace=True)
        self.assertTrue(out.startswith("REFUSED"))

    def test_stated_fact_silently_supersedes_a_tentative_guess(self):
        self.guessed("places", "lives_in", "Denver", "model-a")
        self.said("places", "lives_in", "Boston")
        self.assertFalse(self.fact("lives_in", "Denver").open)

    def test_multi_valued_predicates_do_not_conflict(self):
        self.said("music", "likes", "The Beatles")
        out = self.said("music", "likes", "Radiohead")
        self.assertTrue(out.startswith("saved"))

    def test_near_duplicates_are_caught(self):
        self.said("music", "likes", "The Beatles")
        out = self.said("music", "likes", "the beatles band")
        self.assertIn("POSSIBLE DUPLICATE", out)
        self.assertTrue(self.said("music", "likes", "the beatles band", force=True).startswith("saved"))

    def test_same_value_reinforces(self):
        self.guessed("music", "likes", "jazz", "model-a")
        out = self.guessed("music", "likes", "jazz", "model-b")
        self.assertIn("reinforced", out)


class CapsuleTests(Base):
    def test_capsule_stays_within_budget_as_memory_grows(self):
        sizes = []
        for i in range(400):
            self.said(f"topic{i % 12}", f"pred{i % 37}", f"value number {i} with some words", force=True)
            if i % 100 == 99:
                sizes.append(self.mem.capsule(budget=600, with_stats=True)[1]["tokens"])
        self.assertTrue(all(s <= 600 for s in sizes), sizes)

    def test_capsule_is_deterministic_within_a_day(self):
        self.said("food", "diet", "vegetarian")
        a = self.mem.capsule()
        self.clock.advance(0.4)
        self.mem.recall("diet")
        self.assertEqual(a, self.mem.capsule())

    def test_capsule_contains_map_and_marks_tentative(self):
        self.said("food", "diet", "vegetarian")
        self.guessed("style", "tone", "dry humour", "model-a")
        text = self.mem.capsule()
        self.assertIn("## Map", text)
        self.assertIn("tone=dry humour?", text)
        self.assertIn("diet=vegetarian", text)

    def test_faded_facts_give_way_and_become_spot_checks(self):
        self.said("work", "employer", "Nimbus Labs", importance=3)
        self.clock.advance(365 * 3)
        for i in range(60):
            self.said("misc", "likes", f"fresh thing {i}", force=True)
        text = self.mem.capsule(budget=350)
        self.assertIn("## Check", text)
        self.assertIn("employer=Nimbus Labs", text.split("## Check")[1])

    def test_private_hidden_but_pinned_private_shown(self):
        self.said("health", "condition", "asthma")
        self.said("health", "allergy", "shellfish", pin=True)
        text = self.mem.capsule()
        self.assertIn("allergy=shellfish", text)
        self.assertNotIn("asthma", text)
        self.assertNotIn("condition=asthma", self.mem.recall("asthma"))
        self.assertIn("1 private item", self.mem.recall("asthma"))
        self.assertIn("condition=asthma", self.memory(clearance="private").recall("asthma"))

    def test_token_estimate_is_sane(self):
        self.assertTrue(8 <= estimate_tokens("The quick brown fox jumps over the lazy dog.") <= 13)


class RecallTests(Base):
    def test_keyword_topic_and_id_lookup(self):
        self.said("travel", "seat_pref", "aisle, never middle")
        self.mem.note("travel", "Hates red-eye flights after the Lisbon trip", "user_stated", evidence="x")
        self.assertIn("aisle", self.mem.recall("seat"))
        self.assertIn("red-eye", self.mem.recall("flights"))
        self.assertIn("aisle", self.mem.recall(topic="travel"))
        self.assertIn("aisle", self.mem.recall("travel"))
        key = fact_key("me", "seat_pref", "aisle, never middle")
        self.assertIn("witness", self.mem.recall(key))
        self.assertIn("travel", self.mem.recall())

    def test_empty_result_says_ask_dont_guess(self):
        self.assertIn("Ask the user rather than guessing", self.mem.recall("favourite colour"))

    def test_review_lists_inferences_and_consolidation(self):
        self.guessed("style", "tone", "dry humour", "model-a")
        for i in range(6):
            self.mem.note("fitness", f"went to the gym, session {i}", "user_stated", evidence="x")
        review = self.mem.review()
        self.assertIn("Unconfirmed inferences", review)
        self.assertIn("fitness: 6 notes", review)


class GitTests(Base):
    git = True

    def setUp(self):
        if not Store._git_available():
            self.skipTest("git not installed")
        super().setUp()

    def test_every_write_is_a_commit_and_verify_catches_edits(self):
        self.said("food", "diet", "vegetarian")
        log = subprocess.run(["git", "-C", str(self.store.home), "log", "--oneline"], capture_output=True, text=True)
        self.assertIn("food/diet=vegetarian", log.stdout)
        self.assertEqual(self.mem.verify(), [])
        path = self.store.event_files()[0]
        path.write_text(path.read_text().replace("vegetarian", "carnivore"))
        self.store.commit("sneaky edit")
        problems = self.memory().verify()
        self.assertTrue(any("edited" in p for p in problems))
        self.assertTrue(any("append-only violation" in p for p in problems))


class StoreTests(Base):
    def test_fresh_clone_without_events_dir_is_a_store(self):
        shutil.rmtree(self.store.events_dir)  # git does not carry empty directories
        self.assertTrue(Store(self.store.home).exists())
        self.assertTrue(self.said("food", "diet", "vegan").startswith("saved"))

    def test_hand_written_line_without_id_is_accepted(self):
        self.said("food", "diet", "vegan")
        with open(self.store.event_files()[0], "a") as fh:
            fh.write(json.dumps({"t": "2026-01-02T00:00:00Z", "op": "assert", "s": "me", "p": "likes",
                                 "o": "tea", "topic": "food", "src": "U", "by": "hand", "conf": 0.9}) + "\n")
        self.assertIn("likes=tea", self.memory().recall("tea"))
        self.assertEqual(self.memory().verify(), [])


class EntityTests(Base):
    def test_introduction_creates_a_person_with_relation(self):
        out = self.said("family", "sister", "Rebecca")
        self.assertIn("new person: Rebecca [rebecca]", out)
        e = self.mem.people()["rebecca"]
        self.assertEqual((e.kind, e.relations), ("person", ["sister"]))
        self.assertIn("new pet: Miso", self.said("family", "dog", "Miso"))

    def test_relations_and_nicknames_resolve_on_write_and_read(self):
        self.said("family", "sister", "Rebecca")
        self.said("family", "school", "Lincoln Middle School", subject="my little sis", subject_aliases=["Becca"])
        self.said("family", "likes", "horses", subject="Becca")
        e = self.mem.people()["rebecca"]
        self.assertEqual(sorted(f.o for f in e.facts), ["Lincoln Middle School", "horses"])
        for query in ("my sister", "Becca", "what should I get my sister for her birthday?"):
            self.assertIn("Lincoln Middle School", self.mem.recall(query), query)

    def test_unknown_relation_asks_for_a_name_instead_of_guessing(self):
        self.assertTrue(self.said("family", "school", "X", subject="my brother").startswith("UNKNOWN"))
        self.assertIn("No brother on file", self.mem.recall("gift for my brother"))

    def test_two_people_fitting_a_mention_is_ambiguous(self):
        self.said("family", "sister", "Rebecca")
        self.said("family", "sister", "Anna")
        self.assertIn("AMBIGUOUS", self.mem.recall("my sister"))
        self.assertTrue(self.said("family", "likes", "tea", subject="my sister").startswith("AMBIGUOUS"))
        self.assertTrue(self.said("family", "likes", "tea", subject="Anna").startswith("saved"))

    def test_same_name_different_relationship_is_a_different_person(self):
        self.said("family", "sister", "Rebecca")
        out = self.said("work", "team", "platform", subject="Rebecca", subject_relation="coworker")
        self.assertIn("rebecca_coworker", out)
        self.assertIn("AMBIGUOUS", self.mem.recall("Rebecca"))
        self.assertIn("your sister", self.mem.recall("rebecca"))  # exact id is unambiguous

    def test_old_time_sensitive_details_are_flagged_and_confirming_clears_them(self):
        self.said("family", "sister", "Rebecca")
        self.said("family", "school", "Lincoln Middle School", subject="Rebecca")
        self.said("family", "age", "11", subject="Rebecca")
        self.said("family", "likes", "horses", subject="Rebecca")
        self.clock.advance(730)
        out = self.mem.recall("my sister")
        self.assertIn("age ≈13", out)
        self.assertIn("school=Lincoln Middle School (as of 2026-01", out)
        self.assertIn("likes=horses (as of", out)  # a child's interests change fast
        self.assertIn("If it doesn't matter, don't ask", out)
        self.assertIn("school=Lincoln Middle School (as of", self.mem.capsule())
        self.assertIn("Possibly outdated", self.mem.review())
        self.said("family", "school", "Roosevelt High", subject="rebecca", replace=True)
        out = self.mem.recall("rebecca")
        self.assertIn("school=Roosevelt High\n", out)  # current, and no longer flagged
        self.assertIn("earlier: school=Lincoln Middle School", out)

    def test_relationships_change_with_history(self):
        self.said("family", "partner", "Sam")
        self.clock.advance(400)
        self.assertTrue(self.said("family", "partner", "Jordan").startswith("CONFLICT"))
        self.said("family", "partner", "Jordan", replace=True)
        people = self.mem.people()
        self.assertEqual(people["jordan"].relations, ["partner"])
        self.assertEqual(people["sam"].relations, [])
        self.assertIn("formerly: your partner (2026-01→2027-02)", self.mem.recall("Sam"))
        self.assertIn("Jordan: partner", self.mem.capsule())

    def test_birthday_gives_exact_age(self):
        self.said("family", "sister", "Rebecca")
        self.said("family", "birthday", "2012-05-14", subject="Rebecca")
        self.assertIn("age 13 (born 2012-05-14)", self.mem.recall("Rebecca"))

    def test_identity_facts_stay_out_of_active_and_map(self):
        self.said("family", "sister", "Rebecca")
        self.said("family", "likes", "horses", subject="Rebecca")
        text = self.mem.capsule()
        self.assertIn("## People & things\nRebecca: sister", text)
        self.assertIn("Rebecca.likes=horses", text)
        self.assertNotIn("relation_to_me", text)
        self.assertNotIn("people", text.split("## Map")[1])

    def test_notes_about_someone_show_in_their_dossier(self):
        self.said("family", "sister", "Rebecca")
        self.mem.note("family", "wants to learn guitar", "user_stated", evidence="x", subject="my sister")
        self.assertIn("wants to learn guitar", self.mem.recall("Rebecca"))

    def test_me_gives_the_self_profile(self):
        self.said("food", "diet", "vegetarian")
        self.said("work", "employer", "Nimbus")
        self.said("work", "employer", "Acme", replace=True)
        out = self.mem.recall("me")
        self.assertIn("the user [me]", out)
        self.assertIn("employer=Acme", out)
        self.assertIn("earlier: employer=Nimbus", out)

    def test_mcp_recall_description_lists_known_people(self):
        self.said("family", "sister", "Rebecca")
        tools = Server(self.mem, out=io.StringIO()).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertIn("Rebecca: sister", tools[0]["description"])


class DescriptiveLookupTests(Base):
    def setUp(self):
        super().setUp()
        self.said("hobbies", "coaches", "Hawks U12 soccer")
        self.said("family", "sister", "Rebecca")
        self.said("family", "age", "13", subject="Rebecca")
        for name, pos, age in (("Leo", "goalkeeper", "11"), ("Sam", "striker", None)):
            self.said("sports", "plays_for", "Hawks U12 soccer", subject=name, subject_relation="player")
            self.said("sports", "position", pos, subject=name)
            if age:
                self.said("sports", "age", age, subject=name)
        self.mem.note("sports", "Leo made a great save; his dad Marco drove carpool", "user_stated", evidence="x")

    def test_description_resolves_through_shared_values_and_age(self):
        out = self.mem.recall("the kid I coach")
        self.assertTrue(out.startswith("LIKELY: 'the kid I coach' is probably Leo"), out)
        self.assertIn("coaches Hawks U12 soccer (shared with Leo)", out)
        self.assertIn("confirm in passing", out)

    def test_rare_words_outweigh_common_ones(self):
        self.assertIn("probably Sam", self.mem.recall("the kid who plays striker"))

    def test_unmatched_details_lower_certainty(self):
        self.assertFalse(self.mem.recall("the kid I coach who loves dinosaurs").startswith("LIKELY"))

    def test_unknown_name_reports_where_it_is_mentioned(self):
        out = self.mem.recall("Marco")
        self.assertIn("Marco has no profile yet", out)
        self.assertIn("mentioned alongside: Leo", out)

    def test_possessive_relation_is_not_an_age_description(self):
        self.assertTrue(self.mem.recall("my kid's dentist").startswith("No child on file"))

    def test_writes_about_a_description_ask_first(self):
        out = self.said("sports", "needs", "new gloves", subject="the goalkeeper I coach")
        self.assertTrue(out.startswith("UNSURE, not saved"), out)
        self.assertIn("subject='leo'", out)
        self.said("sports", "needs", "new gloves", subject="leo", subject_aliases=["the goalkeeper I coach"])
        self.assertIn("needs=new gloves", self.mem.recall("the goalkeeper I coach"))

    def test_connections_between_people_show_in_both_files(self):
        self.said("family", "best_friend", "Maya", subject="Rebecca")
        self.said("family", "plays", "violin", subject="Maya", subject_relation="sister's friend")
        self.assertIn("Rebecca best friend them", self.mem.recall("Maya"))
        self.assertIn("best friend Maya [maya]", self.mem.recall("rebecca"))


class BeliefTests(Base):
    def propose(self, text="Prefers async over meetings", **kw):
        out = self.mem.reflect("propose", text=text, model=kw.pop("model", "claude"), reason="observed", **kw)
        return out.split("]")[0].split("[")[1]

    def test_evidence_moves_confidence_and_distinct_models_count_more(self):
        bid = self.propose()
        b = lambda: self.mem.graph.beliefs[bid]
        start = b().conf
        self.mem.reflect("support", id=bid, reason="declined a call", model="claude")
        first = b().conf
        self.mem.reflect("support", id=bid, reason="declined another call", model="claude")
        repeat = b().conf
        self.mem.reflect("support", id=bid, reason="asked for a doc instead", model="gpt-5")
        self.assertGreater(first, start)
        self.assertGreater(b().conf - repeat, repeat - first)  # a second model's view counts more than a repeat
        self.mem.reflect("contradict", id=bid, reason="scheduled a meeting himself", model="gemini")
        self.assertLess(b().conf, 0.8)

    def test_models_alone_never_reach_certainty(self):
        bid = self.propose()
        for i in range(12):
            self.mem.reflect("support", id=bid, reason=f"obs {i}", model=f"model-{i}")
        self.assertLessEqual(self.mem.graph.beliefs[bid].conf, 0.85)
        self.assertEqual(self.mem.graph.beliefs[bid].status, "open")

    def test_only_the_user_confirms_or_refutes(self):
        bid = self.propose()
        self.assertIn("REFUSED", self.mem.amend(bid, "confirm"))
        self.mem.amend(bid, "retract", evidence="no, I actually like meetings")
        self.assertEqual(self.mem.graph.beliefs[bid].status, "refuted")
        self.assertNotIn("Prefers async", self.mem.capsule())
        self.assertIn('user said "no, I actually like meetings"', self.mem.recall(bid))

    def test_revision_keeps_lineage(self):
        old = self.propose()
        out = self.mem.reflect("revise", id=old, text="Prefers async for status, meetings for decisions",
                               reason="nuance", model="claude")
        new = out.split("→ [")[1].split("]")[0]
        self.assertEqual(self.mem.graph.beliefs[old].status, "revised")
        self.assertIn("revised from: [" + old, self.mem.recall(new))

    def test_similar_hypotheses_are_merged_not_duplicated(self):
        self.propose()
        out = self.mem.reflect("propose", text="prefers async communication over meetings", model="gpt-5")
        self.assertTrue(out.startswith("SIMILAR"), out)

    def test_beliefs_about_people_and_in_capsule_and_search(self):
        self.said("sports", "plays_for", "Hawks", subject="Leo", subject_relation="player")
        self.propose("Leo's confidence dips after conceding goals", about="Leo")
        self.propose()
        text = self.mem.capsule()
        self.assertIn("## Understanding", text)
        self.assertIn("Prefers async over meetings (0.40, new)", text)
        self.assertIn("believed:", self.mem.recall("Leo"))
        self.assertIn("belief", self.mem.recall("meetings"))
        self.assertIn("UNKNOWN", self.mem.reflect("propose", about="Zed", text="x"))


class LearnedShelfLifeTests(Base):
    def test_shelf_life_learned_from_this_users_history(self):
        for company in ("A Corp", "B Corp", "C Corp"):
            self.said("work", "employer", company, replace=True)
            self.clock.advance(400)  # changes jobs every ~13 months
        self.said("work", "employer", "D Corp", replace=True)
        self.clock.advance(320)  # default for employer is 730 days; learned is 0.75 × 400 = 300
        self.assertIn("Possibly outdated", self.mem.review())
        self.assertIn("employer=D Corp", self.mem.review().split("Possibly outdated")[1])
        self.assertIn("employer=D Corp (as of", self.mem.capsule())


class McpTests(Base):
    def rpc(self, server, method, params=None, mid=1):
        return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_handshake_tools_and_topic_map_in_description(self):
        self.said("food", "diet", "vegetarian")
        out = io.StringIO()
        server = Server(self.mem, out=out)
        init = self.rpc(server, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-code"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("diet=vegetarian", init["result"]["instructions"])
        tools = self.rpc(server, "tools/list")["result"]["tools"]
        recall = next(t for t in tools if t["name"] == "recall")
        self.assertIn("food(1: diet)", recall["description"])
        self.assertEqual([t["name"] for t in tools], ["recall", "remember", "reflect", "amend"])

        res = self.rpc(server, "tools/call", {"name": "remember", "arguments": {
            "topic": "travel", "predicate": "seat_pref", "value": "aisle", "source": "user_stated",
            "evidence": "always aisle", "model": "claude-opus-5-5"}})["result"]
        self.assertFalse(res["isError"])
        self.assertIn("tools/list_changed", out.getvalue())  # new topic → map in description refreshed
        f = self.mem.graph.facts[fact_key("me", "seat_pref", "aisle")]
        self.assertEqual(f.contributors, ["claude-opus-5-5"])

        res = self.rpc(server, "tools/call", {"name": "recall", "arguments": {"query": "seat"}})["result"]
        self.assertIn("aisle", res["content"][0]["text"])
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIn("error", self.rpc(server, "nope/nope"))

    def test_serve_loop_over_stdio(self):
        out = io.StringIO()
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]
        Server(self.mem, out=out).serve(io.StringIO("\n".join(lines) + "\n"))
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])


if __name__ == "__main__":
    unittest.main()
