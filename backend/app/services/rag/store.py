"""Where vectors live, and how nearest-neighbour search actually works.

Two implementations behind one interface, on purpose:

* `InMemoryStore` does the similarity maths in plain Python — about ten
  lines. Reading it shows exactly what a vector database does, and it means
  the test suite needs no Postgres.
* `PgVectorStore` does the same thing in Postgres with pgvector and an HNSW
  index, which is how it's done when the corpus stops fitting in RAM.

The interesting difference is *exactness*. The in-memory store compares the
query against every row, so its answer is exactly right and it costs O(n).
HNSW is an approximate index: it walks a navigable small-world graph and can
miss a true neighbour, trading a little recall for enormous speed. At 58
chunks that trade is pointless; at ten million it's the only option. Having
both makes the approximation measurable — run the evaluation against each and
compare recall.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Protocol, Sequence

from sqlalchemy import text as sql
from sqlalchemy.orm import Session as DBSession

from app.services.rag.chunking import Chunk

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk and why it was returned."""

    chunk: Chunk
    score: float
    # Which retrieval arm found it, for debugging fusion: "vector",
    # "keyword", or "vector+keyword" once fused.
    via: str = "vector"


class VectorStore(Protocol):
    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int: ...

    def search(
        self, vector: Sequence[float], k: int, age_group: str | None = None,
    ) -> list[Hit]: ...

    def keyword_search(
        self, query: str, k: int, age_group: str | None = None,
    ) -> list[Hit]: ...

    def count(self) -> int: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, computed the long way.

    Our embedders return unit vectors, so the dot product alone would do.
    The division is kept so the function is correct for any input and so the
    formula is visible: similarity is the cosine of the angle between two
    vectors, which ignores magnitude and compares direction only. That's why
    a two-sentence chunk can match a long one.
    """
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class InMemoryStore:
    """Exact brute-force search. The reference implementation."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[Chunk, list[float]]] = {}

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must be the same length")
        for c, v in zip(chunks, vectors):
            # Keyed by content hash, so re-ingesting the same corpus
            # replaces rather than duplicating.
            self._rows[c.chunk_id] = (c, list(v))
        return len(chunks)

    def count(self) -> int:
        return len(self._rows)

    def _candidates(self, age_group: str | None):
        for chunk, vec in self._rows.values():
            # Metadata filter applied BEFORE scoring. This is a correctness
            # filter, not a ranking tweak: the grade 9-12 explanation of
            # compound interest must never be returned for a 4-year-old,
            # however similar it looks.
            if age_group and age_group not in chunk.age_groups:
                continue
            yield chunk, vec

    def search(
        self, vector: Sequence[float], k: int, age_group: str | None = None,
    ) -> list[Hit]:
        scored = [
            Hit(chunk=c, score=cosine(vector, v), via="vector")
            for c, v in self._candidates(age_group)
        ]
        scored.sort(key=lambda h: -h.score)
        return scored[:k]

    def keyword_search(
        self, query: str, k: int, age_group: str | None = None,
    ) -> list[Hit]:
        """Term-overlap scoring, standing in for BM25.

        Deliberately crude: its job is to catch exact tokens that embeddings
        blur — "401(k)", "FICA", a specific dollar figure. Vector search is
        good at meaning and bad at rare literals, which is the whole argument
        for hybrid retrieval.
        """
        import re

        terms = {t for t in re.findall(r"[a-z0-9']+", query.lower()) if len(t) > 2}
        if not terms:
            return []
        hits: list[Hit] = []
        for chunk, _ in self._candidates(age_group):
            words = re.findall(r"[a-z0-9']+", chunk.embed_text.lower())
            if not words:
                continue
            overlap = sum(1 for w in words if w in terms)
            if overlap:
                # Normalised by length so a long chunk doesn't win on volume.
                hits.append(Hit(
                    chunk=chunk,
                    score=overlap / math.sqrt(len(words)),
                    via="keyword",
                ))
        hits.sort(key=lambda h: -h.score)
        return hits[:k]


# ---------------------------------------------------------------------------
# pgvector
# ---------------------------------------------------------------------------

# `vector(N)` is fixed-width, so the dimension count is part of the schema:
# changing embedding model means a migration, not just a re-ingest. That is
# the main operational cost of a vector store and worth knowing up front.
DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id     VARCHAR(32) PRIMARY KEY,
    text         TEXT        NOT NULL,
    embed_text   TEXT        NOT NULL,
    source_file  VARCHAR(200) NOT NULL,
    band         VARCHAR(20) NOT NULL,
    section      TEXT        NOT NULL,
    kind         VARCHAR(20) NOT NULL,
    age_groups   JSONB       NOT NULL,
    embedding    vector({dims}) NOT NULL,
    created_at   TIMESTAMP   NOT NULL DEFAULT now()
);

-- HNSW over cosine distance. Approximate: it can miss a true neighbour, in
-- exchange for search time that grows logarithmically rather than linearly.
-- `vector_cosine_ops` must match the operator used at query time (<=>) or
-- the index is silently ignored and every query becomes a seq scan.
CREATE INDEX IF NOT EXISTS ix_rag_chunks_embedding
    ON rag_chunks USING hnsw (embedding vector_cosine_ops);

-- Full-text index for the keyword arm of hybrid search.
CREATE INDEX IF NOT EXISTS ix_rag_chunks_fts
    ON rag_chunks USING gin (to_tsvector('english', embed_text));
"""


class PgVectorStore:
    """pgvector-backed store. Requires Postgres with the extension."""

    def __init__(self, db: DBSession, dimensions: int) -> None:
        self._db = db
        self._dims = dimensions

    def create_schema(self) -> None:
        for statement in DDL.format(dims=self._dims).split(";"):
            if statement.strip():
                self._db.execute(sql(statement))
        self._db.commit()

    @staticmethod
    def _literal(vector: Sequence[float]) -> str:
        """pgvector's text input form: '[0.1,0.2,...]'."""
        return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must be the same length")
        for c, v in zip(chunks, vectors):
            self._db.execute(sql("""
                INSERT INTO rag_chunks (chunk_id, text, embed_text, source_file,
                                        band, section, kind, age_groups, embedding)
                VALUES (:id, :text, :embed_text, :src, :band, :section, :kind,
                        CAST(:ages AS jsonb), CAST(:emb AS vector))
                ON CONFLICT (chunk_id) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    embed_text = EXCLUDED.embed_text
            """), {
                "id": c.chunk_id, "text": c.text, "embed_text": c.embed_text,
                "src": c.source_file, "band": c.band, "section": c.section,
                "kind": c.kind, "ages": json.dumps(list(c.age_groups)),
                "emb": self._literal(v),
            })
        self._db.commit()
        return len(chunks)

    def count(self) -> int:
        return self._db.execute(sql("SELECT count(*) FROM rag_chunks")).scalar_one()

    def _row_to_chunk(self, row) -> Chunk:
        ages = row.age_groups
        return Chunk(
            text=row.text, source_file=row.source_file, band=row.band,
            section=row.section, kind=row.kind,
            age_groups=tuple(ages if isinstance(ages, list) else json.loads(ages)),
        )

    def search(
        self, vector: Sequence[float], k: int, age_group: str | None = None,
    ) -> list[Hit]:
        # `<=>` is cosine DISTANCE (0 = identical), so similarity is 1 - it.
        # Using a different operator than the index was built with would
        # make Postgres ignore the index entirely.
        rows = self._db.execute(sql(f"""
            SELECT *, 1 - (embedding <=> CAST(:emb AS vector)) AS score
            FROM rag_chunks
            {"WHERE age_groups @> CAST(:ages AS jsonb)" if age_group else ""}
            ORDER BY embedding <=> CAST(:emb AS vector)
            LIMIT :k
        """), {
            "emb": self._literal(vector), "k": k,
            **({"ages": json.dumps([age_group])} if age_group else {}),
        }).mappings().all()
        return [Hit(chunk=self._row_to_chunk(r), score=float(r["score"]), via="vector")
                for r in rows]

    def keyword_search(
        self, query: str, k: int, age_group: str | None = None,
    ) -> list[Hit]:
        """Postgres full-text search — real lexical ranking via ts_rank."""
        rows = self._db.execute(sql(f"""
            SELECT *, ts_rank(to_tsvector('english', embed_text),
                              plainto_tsquery('english', :q)) AS score
            FROM rag_chunks
            WHERE to_tsvector('english', embed_text) @@ plainto_tsquery('english', :q)
            {"AND age_groups @> CAST(:ages AS jsonb)" if age_group else ""}
            ORDER BY score DESC
            LIMIT :k
        """), {
            "q": query, "k": k,
            **({"ages": json.dumps([age_group])} if age_group else {}),
        }).mappings().all()
        return [Hit(chunk=self._row_to_chunk(r), score=float(r["score"]), via="keyword")
                for r in rows]
