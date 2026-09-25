"""Zero-dependency lexical retrieval (BM25).

Embeddings are deliberately not required. The caller is a language model that has
already seen the topic map, including predicate names, so it queries with the
store's own vocabulary. At personal scale (thousands of items), BM25 plus that
vocabulary is enough, and it runs anywhere Python runs.
"""
from __future__ import annotations

import math
import re
from collections import Counter

STOP = frozenset("""
a an the and or of to in on for with at by is are was were be been being i me my mine you your it its this
that these those do does did what which who whom how when where why about any some has have had not no
from as but so if than then there their they them he she his her we our us can could should would will
""".split())


def stem(word: str) -> str:
    for suffix, repl, min_len in (("ies", "y", 5), ("ing", "", 6), ("ed", "", 5), ("es", "", 5), ("s", "", 4)):
        if word.endswith(suffix) and len(word) >= min_len and not word.endswith("ss"):
            return word[: -len(suffix)] + repl
    return word


def tokenize(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower().replace("_", " ")) if w not in STOP]


def jaccard(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


class Index:
    def __init__(self, docs: list[tuple[str, str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids = [d[0] for d in docs]
        self.tfs = [Counter(tokenize(d[1])) for d in docs]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        self.df = Counter()
        for tf in self.tfs:
            self.df.update(tf.keys())
        self.vocab = set(self.df)

    def _expand(self, term: str) -> list[str]:
        """Exact term, else vocabulary words sharing a 4+ char prefix (veg -> vegetarian)."""
        if term in self.vocab or len(term) < 3:
            return [term]
        return [v for v in self.vocab if v.startswith(term) or (len(v) >= 4 and term.startswith(v))] or [term]

    def search(self, query: str) -> list[tuple[str, float]]:
        n = len(self.ids)
        terms = [t for q in tokenize(query) for t in self._expand(q)]
        scores = []
        for i, tf in enumerate(self.tfs):
            score = 0.0
            for t in terms:
                if t not in tf:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                denom = tf[t] + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg)
                score += idf * tf[t] * (self.k1 + 1) / denom
            if score > 0:
                scores.append((self.ids[i], score))
        return sorted(scores, key=lambda x: -x[1])
