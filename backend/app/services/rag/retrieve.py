"""Hybrid retrieval: vector search and keyword search, fused.

Why both arms, concretely:

* Vector search understands meaning. "How do I explain saving?" finds a
  passage about setting money aside even with no shared words. It is weak on
  rare literals — "401(k)", "FICA", "$7.25" — because a token seen rarely in
  training gets a vague position in the space.
* Keyword search is the mirror image: exact on literals, blind to meaning.
  It cannot connect "saving" to "setting money aside" at all.

Fusing them with Reciprocal Rank Fusion gives a result that is rarely worse
than either arm and usually better.

RRF is used rather than adding the scores, because the two arms' scores are
not comparable: cosine similarity sits in roughly 0.3-0.9 while `ts_rank`
returns small numbers near zero. Normalising them against each other means
picking a weighting with no principled basis, and the weighting then drifts
as the corpus changes. RRF throws the scores away and keeps only the
ordering:

    score(d) = sum over arms of  1 / (K + rank(d))

A document ranked 1st by either arm scores 1/(60+1); one ranked 1st by both
scores twice that. K dampens the top ranks so a single arm's confident-but-
wrong first place can't dominate; 60 is the value from the original paper and
is a sane default rather than a tuned one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.rag.embeddings import Embedder
from app.services.rag.store import Hit, VectorStore

log = logging.getLogger(__name__)

# From Cormack et al. (2009). Larger flattens the contribution of top ranks.
RRF_K = 60
# Each arm fetches more than we return, so fusion has material to reorder.
# Too small and a document found 2nd by one arm and 30th by the other is
# never seen as agreement.
OVER_FETCH = 4


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[Hit]
    # Kept for debugging and for the evaluation harness, which compares the
    # arms against the fusion to show whether hybrid actually helped.
    vector_hits: list[Hit]
    keyword_hits: list[Hit]

    @property
    def context(self) -> str:
        """The retrieved chunks as prompt context, each with its citation.

        Sources are attached here rather than left to the generator: the
        model must never be the thing that decides where a fact came from.
        """
        blocks = []
        for i, h in enumerate(self.hits, 1):
            blocks.append(
                f"[{i}] {h.chunk.section} "
                f"(ages {'/'.join(h.chunk.age_groups)}, {h.chunk.source_file})\n"
                f"{h.chunk.text}"
            )
        return "\n\n".join(blocks)


def reciprocal_rank_fusion(
    arms: list[list[Hit]], k: int, rrf_k: int = RRF_K,
) -> list[Hit]:
    """Fuse ranked lists by rank position, ignoring raw scores."""
    scores: dict[str, float] = {}
    best: dict[str, Hit] = {}
    arms_hit: dict[str, set[str]] = {}
    for arm in arms:
        for rank, hit in enumerate(arm, start=1):
            cid = hit.chunk.chunk_id
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank)
            arms_hit.setdefault(cid, set()).add(hit.via)
            # Keep whichever arm ranked it highest, for a readable `via`.
            if cid not in best or rank < 1:
                best.setdefault(cid, hit)
    fused = [
        Hit(
            chunk=best[cid].chunk,
            score=score,
            via="+".join(sorted(arms_hit[cid])),
        )
        for cid, score in scores.items()
    ]
    fused.sort(key=lambda h: -h.score)
    return fused[:k]


def retrieve(
    query: str,
    store: VectorStore,
    embedder: Embedder,
    k: int = 5,
    age_group: str | None = None,
    hybrid: bool = False,
) -> RetrievalResult:
    """Retrieve the `k` most relevant chunks for `query`.

    `age_group` is a hard filter, not a preference: content written for
    teenagers is wrong for a four-year-old no matter how well it matches.

    `hybrid` defaults to OFF, which is the opposite of the usual advice and
    is a measured result rather than a preference. On the golden set with
    voyage-3-lite:

        vector only   precision@5 = 0.950   MRR = 0.917
        hybrid        precision@5 = 0.700   MRR = 0.917

    Fusion cost 26% of precision and bought nothing. The reason is that RRF
    weights every arm equally, so fusing a strong ranker with a weak one
    drags the result toward the weak one — and the keyword arm here is
    crude term overlap, no match for a good embedding model. MRR is
    unchanged, so the first relevant hit stays put; hybrid only pollutes
    positions 2-5.

    Worth revisiting when either changes:
      * the keyword arm becomes real BM25 (or Postgres ts_rank via
        PgVectorStore, which is proper lexical ranking rather than this
        stand-in);
      * RRF becomes weighted, so a weak arm can contribute without
        outvoting a strong one;
      * the queries shift toward rare literals (tickers, "401(k)", FICA),
        where embeddings genuinely blur and the golden set is currently
        thin — only 2 of 8 questions are literal-heavy.

    Keeping both arms implemented and measurable is the point: the answer
    is corpus- and model-specific, so it has to stay re-runnable rather
    than be decided once.
    """
    fetch = k * OVER_FETCH
    # input_type="query" matters — a question and a passage are different
    # shapes of text and the model accounts for that.
    [vector] = embedder.embed([query], input_type="query")
    vector_hits = store.search(vector, fetch, age_group=age_group)
    keyword_hits = store.keyword_search(query, fetch, age_group=age_group) if hybrid else []

    if not hybrid:
        hits = vector_hits[:k]
    else:
        hits = reciprocal_rank_fusion([vector_hits, keyword_hits], k)
    log.info(
        "RAG query %r: %d vector, %d keyword, %d fused (ages=%s)",
        query[:60], len(vector_hits), len(keyword_hits), len(hits), age_group or "any",
    )
    return RetrievalResult(hits=hits, vector_hits=vector_hits, keyword_hits=keyword_hits)
