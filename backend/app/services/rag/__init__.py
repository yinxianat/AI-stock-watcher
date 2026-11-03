"""Retrieval-augmented generation over the FDIC Money Smart curriculum.

The corpus is the FDIC's Parent/Caregiver Guides (public domain, US federal
work) in `backend/RAG_sources/`. They cover the same money concepts at four
reading levels, which is why retrieval here needs metadata filtering and not
just similarity: returning the grade 9-12 explanation of compound interest
for a four-year-old is a correctness failure, not a ranking one.

Pipeline, in order:

    chunking   -> structure-aware splitting, one idea per chunk
    embeddings -> text to vectors (Voyage, or a deterministic fake in tests)
    store      -> VectorStore: in-memory (shows the maths) or pgvector (real)
    retrieve   -> hybrid vector + keyword search, fused, age-band filtered
    evaluate   -> precision@k / recall@k / MRR against a golden set

`evaluate` is the part that makes the rest trustworthy: without it, a change
to chunking or fusion weights is a guess.
"""
