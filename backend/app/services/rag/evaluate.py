"""Measuring whether retrieval is any good.

This is the part most RAG projects skip, and skipping it means every later
decision is a guess. Without numbers, "should chunks be 500 or 1500
characters?" and "is hybrid search worth it?" can only be answered by
reading a few results and forming an impression — which is how systems end
up tuned to whatever query the author happened to try.

The method: a golden set of (question, chunks that should be returned)
pairs, then three standard metrics.

* **precision@k** — of the k returned, what fraction were relevant? Measures
  wasted context. Low precision means the generator is reading noise.
* **recall@k** — of the relevant chunks that exist, what fraction were
  returned? Measures missed answers. Low recall means the generator cannot
  possibly be right, because it was never shown the material.
* **MRR** — 1/rank of the first relevant hit, averaged. Captures ordering,
  which the other two ignore: a relevant chunk at position 1 and at position
  5 score the same on precision@5 but are very different in practice, since
  the model attends most to the start of its context.

Relevance here is judged by which source section a chunk came from, not by
hand-labelling each of the 58 chunks. That keeps the golden set cheap to
maintain as chunking changes — a question is "about" Buying Decisions
regardless of how that section happens to be split today. The cost is
coarseness: it can't distinguish a good chunk from a mediocre one within the
right section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.rag.embeddings import Embedder
from app.services.rag.retrieve import retrieve
from app.services.rag.store import VectorStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoldenQuery:
    """One question and what counts as a right answer for it.

    `expect_sections` matches on substring against a chunk's `section`, and
    `expect_age_group` asserts the metadata filter: a question asked about a
    four-year-old must not be answered with grade 9-12 material, which is a
    correctness property rather than a ranking one.
    """

    question: str
    expect_sections: tuple[str, ...]
    age_group: str | None = None
    expect_age_group: str | None = None
    note: str = ""


@dataclass
class Metrics:
    precision: float = 0.0
    recall: float = 0.0
    mrr: float = 0.0
    age_violations: int = 0
    queries: int = 0
    misses: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "queries": self.queries,
            "precision_at_k": round(self.precision, 3),
            "recall_at_k": round(self.recall, 3),
            "mrr": round(self.mrr, 3),
            "age_violations": self.age_violations,
            "misses": self.misses,
        }


def _relevant(hit, q: GoldenQuery) -> bool:
    section = hit.chunk.section.lower()
    return any(want.lower() in section for want in q.expect_sections)


def evaluate(
    golden: list[GoldenQuery],
    store: VectorStore,
    embedder: Embedder,
    k: int = 5,
    hybrid: bool = True,
) -> Metrics:
    """Run the golden set and return averaged metrics.

    Recall needs a denominator — how many relevant chunks exist at all —
    which brute-force search over the store would give but the interface
    doesn't expose. Instead we retrieve a deep slice (k * 10) and treat the
    relevant chunks found there as the ground-truth set. That slightly
    overstates recall when a relevant chunk ranks below that depth, which is
    worth knowing before quoting the number.
    """
    m = Metrics(queries=len(golden))
    if not golden:
        return m
    p_sum = r_sum = mrr_sum = 0.0
    for q in golden:
        result = retrieve(q.question, store, embedder, k=k,
                          age_group=q.age_group, hybrid=hybrid)
        hits = result.hits
        rel_flags = [_relevant(h, q) for h in hits]

        p_sum += (sum(rel_flags) / len(hits)) if hits else 0.0

        deep = retrieve(q.question, store, embedder, k=k * 10,
                        age_group=q.age_group, hybrid=hybrid).hits
        total_relevant = sum(1 for h in deep if _relevant(h, q))
        r_sum += (sum(rel_flags) / total_relevant) if total_relevant else 0.0

        first = next((i for i, ok in enumerate(rel_flags, start=1) if ok), None)
        mrr_sum += (1.0 / first) if first else 0.0

        if not any(rel_flags):
            m.misses.append(q.question)
        if q.expect_age_group:
            for h in hits:
                if q.expect_age_group not in h.chunk.age_groups:
                    m.age_violations += 1

    n = len(golden)
    m.precision, m.recall, m.mrr = p_sum / n, r_sum / n, mrr_sum / n
    return m


def compare(
    golden: list[GoldenQuery], store: VectorStore, embedder: Embedder, k: int = 5,
) -> dict[str, dict]:
    """Vector-only against hybrid, on the same golden set.

    This is the experiment that justifies the extra machinery. If hybrid
    doesn't beat vector-only on this corpus, the keyword arm is complexity
    with no payoff and should go.
    """
    return {
        "vector_only": evaluate(golden, store, embedder, k=k, hybrid=False).as_dict(),
        "hybrid": evaluate(golden, store, embedder, k=k, hybrid=True).as_dict(),
    }


# ---------------------------------------------------------------------------
# The golden set
# ---------------------------------------------------------------------------
# Written against the eight selected documents. Each entry is a question a
# parent might plausibly ask, paired with the section that ought to answer
# it. The age-filter cases are the ones that matter most: they encode the
# requirement that reading level is a correctness constraint.

GOLDEN_SET: list[GoldenQuery] = [
    GoldenQuery(
        question="What is the difference between a need and a want?",
        expect_sections=("buying decisions",),
        note="Near-verbatim match; a floor for any working retriever.",
    ),
    GoldenQuery(
        question="How do I help my child decide what to buy?",
        expect_sections=("buying decisions",),
        note="Paraphrase — vector search should carry this, keyword alone won't.",
    ),
    GoldenQuery(
        question="What is an entrepreneur?",
        expect_sections=("earn",),
        note="Rare literal; the keyword arm should help.",
    ),
    GoldenQuery(
        question="How do taxes like FICA and Social Security work?",
        expect_sections=("earn",),
        note="Acronyms are exactly where embeddings blur and keywords win.",
    ),
    GoldenQuery(
        question="How should I explain earning money to a preschooler?",
        expect_sections=("earn", "counting coins", "currency"),
        age_group="3-5",
        expect_age_group="3-5",
        note="The filter case: must not return grade 9-12 material.",
    ),
    GoldenQuery(
        question="How do I talk to my teenager about choosing a career?",
        expect_sections=("earn",),
        age_group="13-17",
        expect_age_group="13-17",
        note="Same concept, opposite end of the age range.",
    ),
    GoldenQuery(
        question="What are coins worth and how do you count them?",
        expect_sections=("currency", "counting coins"),
        age_group="3-5",
        expect_age_group="3-5",
    ),
    GoldenQuery(
        question="How does the economy affect my family's money?",
        expect_sections=("earn",),
        note="Abstract phrasing with no shared vocabulary to lean on.",
    ),
]
