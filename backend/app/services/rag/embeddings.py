"""Text to vectors.

An embedding model maps text to a point in high-dimensional space such that
related text lands nearby. "How do I explain saving?" and "Teaching children
to set money aside" share almost no words but end up close together, which
is the whole reason retrieval can beat keyword search.

Two things worth understanding, because both bite in practice:

* **Asymmetry.** A short question and a long passage are different shapes of
  text. Voyage takes an `input_type` of "query" or "document" and embeds them
  into the same space with that difference accounted for. Getting this
  backwards quietly costs accuracy — the vectors are still valid, just worse.

* **Normalisation.** Voyage returns unit-length vectors, so a dot product
  *is* cosine similarity and we never divide by magnitudes. Code that
  assumes this and then meets an un-normalised model gets silently wrong
  rankings, so `embed` asserts it.

`FakeEmbedder` exists so the suite never makes a network call. It is
deterministic and, importantly, *not* random: it hashes words into buckets,
so texts sharing vocabulary really do score closer together. A random fake
would make every retrieval test vacuous.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from typing import Literal, Protocol, Sequence

import httpx

from app.core.settings import get_settings

log = logging.getLogger(__name__)

InputType = Literal["query", "document"]

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=30.0)
# Voyage accepts up to 128 inputs per call. Kept well below that because
# the free tier caps tokens-per-minute as well as requests-per-minute, and
# a 96-chunk batch of ~300-token chunks blows the token cap in one request.
_BATCH = 24
# Retries for 429, which is routine on the free tier rather than an error.
_MAX_RETRIES = 5
_BACKOFF_BASE = 4.0


class EmbeddingError(RuntimeError):
    """The provider refused or was unreachable."""


class Embedder(Protocol):
    """Anything that can turn text into vectors.

    An interface rather than a concrete client so the store and retriever
    never depend on a vendor, and so tests can run offline.
    """

    @property
    def dimensions(self) -> int: ...

    @property
    def name(self) -> str: ...

    def embed(self, texts: Sequence[str], input_type: InputType) -> list[list[float]]: ...


def _assert_unit_length(vectors: list[list[float]], who: str) -> None:
    """Downstream code treats dot product as cosine similarity."""
    for v in vectors[:3]:  # spot-check; checking all would be wasted work
        norm = math.sqrt(sum(x * x for x in v))
        if not 0.99 <= norm <= 1.01:
            raise EmbeddingError(
                f"{who} returned a vector of length {norm:.3f}, expected 1.0 — "
                "cosine similarity would be wrong"
            )


class VoyageEmbedder:
    """Voyage AI, via HTTP. Anthropic's recommended embedding partner."""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        settings = get_settings()
        self._model = model or settings.rag_embedding_model
        self._key = (api_key or settings.voyage_api_key).strip()
        # voyage-3-lite is 512-dimensional; the others are 1024. Wrong
        # dimensions only surface when a query vector meets a stored one, so
        # it's declared up front and checked on the first response.
        self._dims = 512 if "lite" in self._model else 1024

    @property
    def dimensions(self) -> int:
        return self._dims

    @property
    def name(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str], input_type: InputType) -> list[list[float]]:
        if not self._key:
            raise EmbeddingError("VOYAGE_API_KEY is not set")
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            batch = list(texts[start : start + _BATCH])
            out.extend(self._call(batch, input_type))
        _assert_unit_length(out, self._model)
        return out

    def _call(self, batch: list[str], input_type: InputType) -> list[list[float]]:
        # 429 is routine rather than exceptional here: the free tier allows
        # only a few requests a minute, and an evaluation run issues one per
        # query. Without backoff, ingesting a corpus fails halfway and
        # leaves the store half-populated.
        for attempt in range(_MAX_RETRIES + 1):
            try:
                r = httpx.post(
                    _VOYAGE_URL,
                    timeout=_TIMEOUT,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"input": batch, "model": self._model, "input_type": input_type},
                )
            except httpx.HTTPError as e:
                raise EmbeddingError(f"Voyage unreachable: {e}") from e
            if r.status_code != 429:
                break
            if attempt == _MAX_RETRIES:
                raise EmbeddingError(
                    f"Voyage rate limit after {_MAX_RETRIES} retries — "
                    "lower RAG_EMBED_BATCH or wait for the quota to reset"
                )
            # Honour Retry-After when given; otherwise back off exponentially.
            wait = float(r.headers.get("retry-after") or 0) or _BACKOFF_BASE * 2**attempt
            log.warning("Voyage rate limited; retrying in %.0fs", wait)
            time.sleep(wait)
        if r.status_code != 200:
            # The body can echo the input, so only the status is logged.
            log.error("Voyage embedding call failed with status %s", r.status_code)
            raise EmbeddingError(f"Voyage returned {r.status_code}")
        data = r.json().get("data") or []
        vectors = [row["embedding"] for row in data]
        if len(vectors) != len(batch):
            raise EmbeddingError(
                f"asked for {len(batch)} embeddings, got {len(vectors)}"
            )
        if vectors and len(vectors[0]) != self._dims:
            # Better to fail loudly than to store vectors that can never be
            # compared against a query.
            raise EmbeddingError(
                f"{self._model} returned {len(vectors[0])} dimensions, "
                f"expected {self._dims}"
            )
        return vectors


class LocalEmbedder:
    """Free, offline embeddings via model2vec static vectors.

    model2vec distils a transformer into a lookup table, so inference is a
    token lookup plus mean pooling — pure numpy, no torch, no ONNX, no
    network, no quota. That matters here for two reasons beyond cost:

    * it installs on Python 3.14, where torch and onnxruntime wheels are
      not reliably available yet;
    * embeddings in this project are a BUILD-TIME step, so a local model
      costs production nothing. The ~30MB of weights live on the machine
      doing the ingest, not in the Railway container.

    The tradeoff against a hosted model is real: static embeddings have no
    context, so "bank" by a river and "bank" holding money get the same
    vector. For a curriculum corpus about one subject that matters little,
    and the evaluation harness is there to say by how much rather than
    leave it to opinion.
    """

    # 256-dim, ~30MB. The 2M variant is smaller and weaker; 32M is better
    # and slower to download.
    DEFAULT_MODEL = "minishlab/potion-base-8M"

    def __init__(self, model_name: str | None = None) -> None:
        self._name = model_name or get_settings().rag_local_model or self.DEFAULT_MODEL
        self._model = None  # loaded on first use: ~7s, and often never needed

    def _load(self):
        if self._model is None:
            try:
                from model2vec import StaticModel
            except ImportError as e:  # pragma: no cover - dev dependency
                raise EmbeddingError(
                    "model2vec is not installed. It is a build-time "
                    "dependency: pip install model2vec"
                ) from e
            log.info("Loading local embedding model %s", self._name)
            self._model = StaticModel.from_pretrained(self._name)
        return self._model

    @property
    def dimensions(self) -> int:
        return self._load().dim

    @property
    def name(self) -> str:
        return self._name

    def embed(self, texts: Sequence[str], input_type: InputType) -> list[list[float]]:
        """`input_type` is accepted and ignored.

        Static embeddings have no query/document asymmetry to exploit —
        there is no model to condition. Keeping the parameter means this
        class satisfies the same protocol as the hosted ones, so swapping
        backends needs no caller changes.
        """
        if not texts:
            return []
        import numpy as np

        raw = self._load().encode(list(texts))
        # model2vec does not normalise, and the stores treat a dot product
        # as cosine similarity. Normalising here keeps that assumption true
        # for every backend rather than only some.
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # an all-unknown-token text
        return (raw / norms).tolist()


class FakeEmbedder:
    """Deterministic offline embedder for tests.

    Hashes each word into one of `dimensions` buckets and L2-normalises the
    result — a bag-of-words vector. It has no semantic understanding, but it
    is *consistent*: shared vocabulary means higher similarity, which is
    enough to test chunking, storage, fusion and the metric maths without a
    network call. Retrieval *quality* is measured separately against the
    real model by the evaluation harness.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self._dims = dimensions

    @property
    def dimensions(self) -> int:
        return self._dims

    @property
    def name(self) -> str:
        return f"fake-{self._dims}"

    def embed(self, texts: Sequence[str], input_type: InputType) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dims
        for word in re.findall(r"[a-z0-9']+", text.lower()):
            h = hashlib.sha1(word.encode()).digest()
            vec[int.from_bytes(h[:4], "big") % self._dims] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            # An empty or punctuation-only chunk. A zero vector would make
            # every similarity 0, so point it somewhere fixed instead.
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]


def get_embedder() -> Embedder:
    """The configured embedder.

    Outside production, a missing key falls back to the offline fake so the
    pipeline is runnable and testable without credentials.

    In production it raises instead. The fake embedder is bag-of-words with
    no semantic understanding, so falling back there would leave retrieval
    *appearing* to work while returning nonsense — and confidently
    irrelevant answers to a parent are worse than an error, because nobody
    investigates them. A missing key is a deploy mistake and should look
    like one.
    """
    settings = get_settings()
    if settings.rag_embedding_provider == "local":
        return LocalEmbedder()
    if not settings.voyage_api_key.strip():
        if settings.app_env == "production":
            raise EmbeddingError(
                "VOYAGE_API_KEY is not set. Refusing to fall back to the fake "
                "embedder in production: retrieval would return meaningless "
                "results rather than fail."
            )
        log.warning("VOYAGE_API_KEY is not set; using the offline fake embedder")
        return FakeEmbedder()
    # Wrapped: repeated queries (and re-running an evaluation) must not
    # re-pay for vectors we already have.
    return CachedEmbedder(VoyageEmbedder())


class CachedEmbedder:
    """Memoises embeddings by (text, input_type).

    Not an optimisation detail — it changes what is affordable. Evaluating
    the golden set calls `retrieve` four times per question (vector and
    hybrid, each at shallow and deep k), so the same question was embedded
    four times over. On Voyage's free tier that turned an 8-question
    evaluation into 32 throttled requests and it never finished.

    Caching is safe because embedding is a pure function: the same text and
    input_type always map to the same vector. Query vectors are the obvious
    thing to cache in production too, since users ask the same questions.

    The cache is unbounded, which is fine for a golden set and an ingest
    run. A long-lived process serving arbitrary user queries wants an LRU
    bound instead.
    """

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self._cache: dict[tuple[str, str], list[float]] = {}
        self.hits = 0
        self.misses = 0

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    @property
    def name(self) -> str:
        return f"cached({self._inner.name})"

    def embed(self, texts: Sequence[str], input_type: InputType) -> list[list[float]]:
        missing = [t for t in texts if (t, input_type) not in self._cache]
        # Deduplicate within the call as well, so a repeated text in one
        # batch is only sent once.
        unique = list(dict.fromkeys(missing))
        if unique:
            for text, vec in zip(unique, self._inner.embed(unique, input_type)):
                self._cache[(text, input_type)] = vec
        self.misses += len(unique)
        self.hits += len(texts) - len(unique)
        return [self._cache[(t, input_type)] for t in texts]

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "size": len(self._cache)}
