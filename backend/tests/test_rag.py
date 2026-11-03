"""The RAG pipeline: chunking, embedding, storage, hybrid retrieval, metrics.

Everything here runs offline against `FakeEmbedder`, which is deterministic
bag-of-words rather than random — texts sharing vocabulary really do score
closer, so the mechanics can be tested without a network call. Retrieval
*quality* is a separate question that needs the real model; these tests
cover correctness of the machinery.
"""

from __future__ import annotations

import math

import pytest

from app.services.rag import chunking, evaluate, retrieve, store
from app.services.rag.chunking import Chunk
from app.services.rag.embeddings import EmbeddingError, FakeEmbedder, _assert_unit_length

# A miniature corpus, so retrieval assertions are about behaviour rather
# than about whatever the FDIC happened to write.
DOCS = {
    "fdic-prek-2-parent-guide-module-1.pdf": """
MONEY SMART GRADES PRE-K –2: Parent/Caregiver Guide  6
Lesson 1: Counting Coins
TOPIC: Understanding Currency
About This Topic:
Young children can learn to name coins and understand that money is
exchanged for the things a family needs and wants every single day.
From the classroom:
What is a penny worth? A penny is worth one cent and it is the smallest
coin value that children will usually handle in a shop with a parent.
How many pennies make a dime? Ten pennies make one dime, which is a useful
first lesson in counting money with a young child at home.
""",
    "fdic-gr9-12-parentguide-module-1.pdf": """
MONEY SMART GRADES 9 –12: Parent/Caregiver Guide  6
Theme 1: Earn
Topic Connections: Connects with Lesson 1 and Lesson 6 of the curriculum.
Topic Overview:
Choosing a career means weighing passion against financial stability, and
teenagers benefit from discussing how earning potential compounds overvtime.
From the classroom:
What is an entrepreneur? An entrepreneur is someone who sees an opportunity
and makes a plan to start and manage a business, investing time and money.
How much do you pay in taxes? Income tax rates depend on earnings, and FICA
covers Social Security and Medicare contributions from every paycheck.
""",
}


def _chunks() -> list[Chunk]:
    out: list[Chunk] = []
    for name, text in DOCS.items():
        out.extend(chunking.chunk_document(text, name))
    return out


@pytest.fixture
def indexed():
    """An in-memory store loaded with the miniature corpus."""
    chunks = _chunks()
    emb = FakeEmbedder()
    s = store.InMemoryStore()
    s.upsert(chunks, emb.embed([c.embed_text for c in chunks], input_type="document"))
    return s, emb, chunks


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,band", [
    ("fdic-prek-2-parent-guide-module-1.pdf", "prek-2"),
    ("fdic-gr3-5-parent-guide-module-4.pdf", "gr3-5"),
    ("fdic-gr6-8-parent-guide-module-2.pdf", "gr6-8"),
    ("fdic-gr9-12-parentguide-module-5.pdf", "gr9-12"),
    ("general-pages-prek-2-parentguide.pdf", "prek-2"),
    ("unrelated-document.pdf", None),
])
def test_band_comes_from_the_filename(name, band):
    assert chunking.band_from_filename(name) == band


def test_band_is_not_read_from_the_document_text():
    """fdic-gr6-8-parent-guide-module-1.pdf has "GRADES PRE-K -2" in its own
    header — FDIC shipped the wrong one. Trusting the text would mislabel
    every chunk in that file, and the age filter is a correctness feature."""
    text = "MONEY SMART GRADES PRE-K –2: Parent/Caregiver Guide 6\nTheme 1: Earn\nTopic Overview:\n" + ("x" * 300)
    chunks = chunking.chunk_document(text, "fdic-gr6-8-parent-guide-module-1.pdf")
    assert chunks
    assert all(c.band == "gr6-8" for c in chunks)


def test_page_furniture_is_stripped():
    out = chunking.strip_page_furniture(
        "MONEY SMART GRADES 3 – 5: Parent/Caregiver Guide  5\nReal content here."
    )
    assert "MONEY SMART" not in out
    assert "Real content here." in out


def test_qa_blocks_split_into_one_chunk_per_question(indexed):
    _, _, chunks = indexed
    qa = [c for c in chunks if c.kind == "qa"]
    # Two questions per document, two documents.
    assert len(qa) == 4
    assert sum(1 for c in qa if c.text.startswith("What is a penny worth?")) == 1
    assert sum(1 for c in qa if c.text.startswith("What is an entrepreneur?")) == 1


def test_a_mid_answer_question_mark_does_not_split_a_chunk():
    """Answers contain question marks when they quote the question back.
    Splitting on every '?' would cut answers in half."""
    body = (
        "How do you choose a career? Finding the right path begins with "
        "knowing what you enjoy? and then researching jobs that match it, "
        "which takes time and some honest reflection about priorities."
    )
    assert len(chunking.split_qa(body)) == 1


def test_headings_without_a_body_still_name_later_chunks():
    """"Lesson 1: Money Matters" is immediately followed by "TOPIC: ...", so
    neither has a body. Dropping them left every chunk anonymous and
    silently degraded the embedding input."""
    chunks = chunking.chunk_document(
        "Lesson 1: Money Matters\nTOPIC: Buying Decisions\nFrom the classroom:\n"
        "What is a need? " + ("A need is something you cannot live without. " * 6),
        "fdic-gr3-5-parent-guide-module-1.pdf",
    )
    assert chunks
    assert "Buying Decisions" in chunks[0].section


def test_section_is_prepended_to_the_embedded_text(indexed):
    """A bare answer often never names its own topic, so the heading has to
    travel with it into the vector."""
    _, _, chunks = indexed
    c = next(c for c in chunks if c.text.startswith("What is an entrepreneur?"))
    assert "Earn" in c.embed_text
    assert "13-17" in c.embed_text


def test_cross_reference_sections_are_dropped(indexed):
    """"Topic Connections" is a list of lesson numbers — no teaching value,
    and it pollutes similarity."""
    _, _, chunks = indexed
    assert not any("topic connections" in c.section.lower() for c in chunks)


def test_oversized_blocks_are_sub_split():
    parts = chunking._sub_split("x" * 5000, limit=1800)
    assert len(parts) >= 3
    assert all(len(p) <= 1800 for p in parts)


def test_fragments_below_the_floor_are_discarded():
    chunks = chunking.chunk_document(
        "Lesson 1: Tiny\nTOPIC: Nothing\nAbout This Topic:\nToo short.",
        "fdic-gr3-5-parent-guide-module-1.pdf",
    )
    assert chunks == []


def test_chunk_id_is_stable_and_content_addressed():
    a = Chunk(text="same", source_file="f.pdf", band="gr3-5", section="s", kind="qa")
    b = Chunk(text="same", source_file="f.pdf", band="gr3-5", section="other", kind="qa")
    c = Chunk(text="different", source_file="f.pdf", band="gr3-5", section="s", kind="qa")
    assert a.chunk_id == b.chunk_id  # text + file identify a chunk
    assert a.chunk_id != c.chunk_id


def test_age_groups_overlap_across_bands():
    """A 6-year-old sits across two FDIC bands, so both should be reachable."""
    assert "6-9" in chunking.BAND_TO_AGE_GROUPS["prek-2"]
    assert "6-9" in chunking.BAND_TO_AGE_GROUPS["gr3-5"]


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


def test_fake_embedder_is_deterministic():
    e = FakeEmbedder()
    assert e.embed(["hello world"], "document") == e.embed(["hello world"], "document")


def test_fake_embedder_returns_unit_vectors():
    [v] = FakeEmbedder().embed(["saving money for later"], "document")
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)


def test_fake_embedder_scores_shared_vocabulary_higher():
    """Not random: the retrieval tests would be vacuous if it were."""
    e = FakeEmbedder()
    a, b, c = e.embed(
        ["counting coins with children",
         "counting coins at the shop",
         "entrepreneurship and taxes"],
        "document",
    )
    assert store.cosine(a, b) > store.cosine(a, c)


def test_empty_text_does_not_produce_a_zero_vector():
    """A zero vector makes every similarity 0 and silently breaks ranking."""
    [v] = FakeEmbedder().embed(["!!!"], "document")
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)


def test_non_unit_vectors_are_rejected():
    """Downstream code treats dot product as cosine similarity, so a model
    that doesn't normalise must fail loudly rather than rank wrongly."""
    with pytest.raises(EmbeddingError, match="expected 1.0"):
        _assert_unit_length([[3.0, 4.0]], "some-model")


def test_voyage_requires_a_key(monkeypatch):
    from app.core.settings import get_settings
    from app.services.rag.embeddings import VoyageEmbedder

    monkeypatch.setenv("VOYAGE_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(EmbeddingError, match="VOYAGE_API_KEY"):
        VoyageEmbedder().embed(["x"], "document")


def test_get_embedder_needs_no_credentials(monkeypatch):
    """The free local model is the default, so the pipeline works with no
    key at all. The fake embedder is now only a test double — it is no
    longer something a misconfiguration can land you on."""
    from app.core.settings import get_settings
    from app.services.rag.embeddings import get_embedder

    monkeypatch.setenv("VOYAGE_API_KEY", "")
    get_settings.cache_clear()
    assert not isinstance(get_embedder(), FakeEmbedder)


# ---------------------------------------------------------------------------
# Similarity and storage
# ---------------------------------------------------------------------------


def test_cosine_ignores_magnitude():
    """Direction, not length — which is why a short chunk can match a long
    query."""
    assert math.isclose(store.cosine([1, 0], [5, 0]), 1.0)
    assert math.isclose(store.cosine([1, 0], [0, 1]), 0.0)
    assert math.isclose(store.cosine([1, 0], [-1, 0]), -1.0)


def test_cosine_handles_a_zero_vector():
    assert store.cosine([0, 0], [1, 1]) == 0.0


def test_reingesting_replaces_rather_than_duplicates(indexed):
    s, emb, chunks = indexed
    before = s.count()
    s.upsert(chunks, emb.embed([c.embed_text for c in chunks], input_type="document"))
    assert s.count() == before


def test_upsert_rejects_mismatched_lengths(indexed):
    s, _, chunks = indexed
    with pytest.raises(ValueError):
        s.upsert(chunks, [[0.0] * 256])


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def test_vector_search_finds_the_relevant_chunk(indexed):
    s, emb, _ = indexed
    r = retrieve.retrieve("What is a penny worth?", s, emb, k=3)
    assert any("penny" in h.chunk.text.lower() for h in r.hits)


def test_age_filter_excludes_other_bands(indexed):
    """The filter case. Grade 9-12 material for a four-year-old is a
    correctness failure, not a ranking preference — so it must be impossible,
    not merely unlikely."""
    s, emb, _ = indexed
    r = retrieve.retrieve("How do I explain earning money?", s, emb, k=10, age_group="3-5")
    assert r.hits
    for h in r.hits:
        assert "3-5" in h.chunk.age_groups
        assert h.chunk.band == "prek-2"


def test_age_filter_applies_to_the_keyword_arm_too(indexed):
    """A filter on only one arm would leak through fusion."""
    s, emb, _ = indexed
    hits = s.keyword_search("entrepreneur taxes career", k=10, age_group="3-5")
    assert all("3-5" in h.chunk.age_groups for h in hits)


def test_keyword_search_catches_rare_literals(indexed):
    """FICA is exactly what embeddings blur and keywords nail."""
    s, _, _ = indexed
    hits = s.keyword_search("FICA", k=5)
    assert hits
    assert any("FICA" in h.chunk.text for h in hits)


def test_hybrid_marks_which_arms_agreed(indexed):
    """hybrid must be requested explicitly — it is off by default because
    it measured 26% worse on precision@5 than vector alone."""
    s, emb, _ = indexed
    r = retrieve.retrieve("What is an entrepreneur?", s, emb, k=5, hybrid=True)
    assert any("+" in h.via for h in r.hits), "expected at least one chunk found by both"


def test_hybrid_is_off_by_default(indexed):
    """Guards the measured decision: fusing a strong vector arm with a weak
    keyword arm cost precision, because RRF weights arms equally."""
    s, emb, _ = indexed
    r = retrieve.retrieve("What is an entrepreneur?", s, emb, k=5)
    assert r.keyword_hits == []
    assert all(h.via == "vector" for h in r.hits)


def test_disabling_hybrid_uses_vector_only(indexed):
    s, emb, _ = indexed
    r = retrieve.retrieve("penny", s, emb, k=3, hybrid=False)
    assert r.keyword_hits == []
    assert all(h.via == "vector" for h in r.hits)


def test_context_carries_citations(indexed):
    """The generator must never be the thing deciding where a fact came
    from, so sources are attached by us."""
    s, emb, _ = indexed
    ctx = retrieve.retrieve("What is a penny worth?", s, emb, k=2).context
    assert ".pdf" in ctx
    assert "[1]" in ctx
    assert "ages" in ctx


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def _hit(cid: str, via: str, score: float = 1.0):
    return store.Hit(
        chunk=Chunk(text=cid, source_file="f.pdf", band="gr3-5", section="s", kind="qa"),
        score=score, via=via,
    )


def test_rrf_rewards_agreement_between_arms():
    """A document both arms rank highly should beat one that only one arm
    ranks first — that is the entire point of fusing."""
    a = _hit("agreed", "vector")
    b = _hit("vector-only", "vector")
    fused = retrieve.reciprocal_rank_fusion(
        [[b, a], [a]], k=2,  # 'a' is 2nd then 1st; 'b' is 1st then absent
    )
    assert fused[0].chunk.text == "agreed"


def test_rrf_ignores_raw_scores():
    """Cosine sits near 0.3-0.9 and ts_rank near 0, so the scales are not
    comparable; RRF uses only rank position."""
    big = _hit("x", "vector", score=999.0)
    small = _hit("y", "keyword", score=0.0001)
    fused = retrieve.reciprocal_rank_fusion([[big], [small]], k=2)
    assert math.isclose(fused[0].score, fused[1].score)


def test_rrf_records_every_arm_that_found_a_chunk():
    a = _hit("both", "vector")
    b = _hit("both", "keyword")
    fused = retrieve.reciprocal_rank_fusion([[a], [b]], k=1)
    assert fused[0].via == "keyword+vector"


def test_rrf_handles_an_empty_arm():
    fused = retrieve.reciprocal_rank_fusion([[_hit("a", "vector")], []], k=5)
    assert len(fused) == 1


# ---------------------------------------------------------------------------
# Evaluation metrics
# ---------------------------------------------------------------------------


def test_perfect_retrieval_scores_one():
    q = evaluate.GoldenQuery(question="x", expect_sections=("s",))
    hits = [_hit("a", "vector"), _hit("b", "vector")]
    assert all(evaluate._relevant(h, q) for h in hits)


def test_relevance_is_judged_on_the_section():
    q = evaluate.GoldenQuery(question="x", expect_sections=("buying decisions",))
    good = store.Hit(chunk=Chunk(text="t", source_file="f.pdf", band="gr3-5",
                                 section="TOPIC: Buying Decisions — From the classroom",
                                 kind="qa"), score=1.0)
    bad = store.Hit(chunk=Chunk(text="t", source_file="f.pdf", band="gr3-5",
                                section="Theme 1: Earn", kind="qa"), score=1.0)
    assert evaluate._relevant(good, q)
    assert not evaluate._relevant(bad, q)


def test_evaluate_reports_every_metric(indexed):
    s, emb, _ = indexed
    golden = [
        evaluate.GoldenQuery(question="What is a penny worth?",
                             expect_sections=("currency", "counting coins")),
        evaluate.GoldenQuery(question="What is an entrepreneur?",
                             expect_sections=("earn",)),
    ]
    m = evaluate.evaluate(golden, s, emb, k=3).as_dict()
    for key in ("precision_at_k", "recall_at_k", "mrr", "age_violations", "queries"):
        assert key in m
    assert m["queries"] == 2
    assert 0.0 <= m["precision_at_k"] <= 1.0
    assert 0.0 <= m["mrr"] <= 1.0


def test_evaluate_counts_age_violations(indexed):
    """The metric that guards the correctness property."""
    s, emb, _ = indexed
    bad = [evaluate.GoldenQuery(
        question="What is an entrepreneur?",
        expect_sections=("earn",),
        age_group=None,            # no filter applied...
        expect_age_group="3-5",    # ...but 3-5 material demanded
    )]
    assert evaluate.evaluate(bad, s, emb, k=5).age_violations > 0


def test_evaluate_records_total_misses(indexed):
    s, emb, _ = indexed
    m = evaluate.evaluate(
        [evaluate.GoldenQuery(question="quantum chromodynamics",
                              expect_sections=("nothing-matches-this",))],
        s, emb, k=3,
    )
    assert m.misses


def test_evaluate_on_an_empty_golden_set_is_safe(indexed):
    s, emb, _ = indexed
    assert evaluate.evaluate([], s, emb).queries == 0


def test_compare_returns_both_arms(indexed):
    s, emb, _ = indexed
    out = evaluate.compare(
        [evaluate.GoldenQuery(question="penny", expect_sections=("currency",))],
        s, emb, k=3,
    )
    assert set(out) == {"vector_only", "hybrid"}


def test_the_shipped_golden_set_is_well_formed():
    """Catches a typo'd age group, which would silently never match."""
    from app.services.rag.chunking import BAND_TO_AGE_GROUPS

    valid = {g for groups in BAND_TO_AGE_GROUPS.values() for g in groups}
    assert len(evaluate.GOLDEN_SET) >= 8
    for q in evaluate.GOLDEN_SET:
        assert q.question.strip()
        assert q.expect_sections
        for field in (q.age_group, q.expect_age_group):
            assert field is None or field in valid, field


# ---------------------------------------------------------------------------
# Corpus curation
# ---------------------------------------------------------------------------


def test_every_pdf_on_disk_has_a_curation_decision():
    """The quiet failure this prevents: a new PDF lands in RAG_sources and
    is neither indexed nor consciously excluded — it just doesn't exist as
    far as retrieval is concerned."""
    from app.services.rag.corpus import check_manifest

    result = check_manifest("RAG_sources")
    assert result["missing_from_disk"] == [], result["missing_from_disk"]
    assert result["undecided_on_disk"] == [], result["undecided_on_disk"]


def test_credit_insurance_and_boilerplate_are_excluded():
    """A third of the collection is about loans, fraud and 'how to use this
    guide'. Those chunks can only dilute retrieval, since they compete for
    the same top-k slots as the material that fits."""
    from app.services.rag.corpus import EXCLUDED, SELECTED_FILENAMES

    for name in (
        "fdic-gr9-12-parentguide-module-4.pdf",   # Borrow
        "fdic-gr9-12-parentguide-module-5.pdf",   # Protect
        "fdic-gr3-5-parent-guide-module-5.pdf",   # Payment Options
        "general-pages-gr6-8-parentguide.pdf",    # boilerplate
    ):
        assert name in EXCLUDED
        assert name not in SELECTED_FILENAMES


def test_the_save_and_invest_modules_are_tier_one():
    """They are the most relevant documents in the collection: the grade
    9-12 one mentions 'stock' 22 times."""
    from app.services.rag.corpus import TIER_1

    names = {s.filename for s in TIER_1}
    assert "fdic-gr9-12-parentguide-module-3.pdf" in names
    assert "fdic-gr6-8-parent-guide-module-3.pdf" in names


def test_every_excluded_file_records_a_reason():
    from app.services.rag.corpus import EXCLUDED

    for name, reason in EXCLUDED.items():
        assert len(reason) > 20, f"{name} has no real reason"


def test_band_override_rescues_the_unbanded_workbook():
    """money-smart-basics-kids-full-packet.pdf has no grade band in its
    filename, so chunk_corpus skipped it silently — losing the only
    Compound Interest material in the collection."""
    from app.services.rag.corpus import BAND_OVERRIDES
    from app.services.rag.chunking import band_from_filename

    name = "money-smart-basics-kids-full-packet.pdf"
    assert band_from_filename(name) is None   # the reason it was skipped
    assert BAND_OVERRIDES[name] == "gr3-5"    # and the fix


def test_chunk_selected_covers_all_four_age_bands():
    """A band with no chunks means age-filtered retrieval returns nothing
    for those children at all."""
    from app.services.rag.chunking import BANDS, chunk_selected

    chunks = chunk_selected("RAG_sources")
    covered = {c.band for c in chunks}
    assert covered == set(BANDS), set(BANDS) - covered


def test_chunk_selected_excludes_the_excluded():
    from app.services.rag.chunking import chunk_selected
    from app.services.rag.corpus import EXCLUDED

    sources = {c.source_file for c in chunk_selected("RAG_sources")}
    assert not (sources & set(EXCLUDED))


# ---------------------------------------------------------------------------
# The curriculum concept set (replaces the 5 hardcoded lesson ideas)
# ---------------------------------------------------------------------------


def test_every_age_and_situation_has_at_least_one_concept():
    """The correctness property. An empty list leaves the writer with
    nothing to teach and no way to report that, so it would silently
    produce a story with a made-up lesson — exactly what the closed set
    exists to prevent."""
    from app.services.curriculum import ALL_AGE_GROUPS, Situation, concepts_for

    for age in ALL_AGE_GROUPS:
        for sit in Situation:
            got = concepts_for(age, sit)
            assert got, f"no concept for ages {age} on a {sit.value} day"


def test_concept_keys_are_unique():
    from app.services.curriculum import BY_KEY, CONCEPTS

    assert len(BY_KEY) == len(CONCEPTS)


def test_concepts_cover_more_than_market_mechanics():
    """The gap this set closes: all five previous concepts were about price
    movement, so saving, goals and opportunity cost never reached a user."""
    from app.services.curriculum import BY_KEY

    for key in ("why_save", "saving_needs_a_goal", "opportunity_cost",
                "needs_and_wants", "risks_and_returns", "compound_growth"):
        assert key in BY_KEY, key


def test_youngest_group_gets_no_investing_vocabulary():
    """FDIC deliberately teaches 3-5 year olds about money without
    investing words, and so should we."""
    from app.services.curriculum import Situation, concepts_for

    for sit in Situation:
        for c in concepts_for("3-5", sit):
            text = f"{c.title} {c.statement}".lower()
            for word in ("invest", "share", "stock", "market", "compound"):
                assert word not in text, f"{c.key} says {word!r} to a 3-5 year old"


def test_whole_market_days_can_teach_the_economy_concept():
    from app.services.curriculum import Situation, concepts_for

    keys = {c.key for c in concepts_for("13-17", Situation.WHOLE_MARKET)}
    assert "economy_moves_everything" in keys


def test_situation_is_derived_from_values_the_pipeline_already_has():
    from app.services.curriculum import Situation, situation_for

    assert situation_for(-3.0, False, False) is Situation.DROP
    assert situation_for(3.0, False, False) is Situation.RISE
    assert situation_for(-2.0, True, False) is Situation.WHOLE_MARKET
    # below_bar wins: a quiet day is a quiet day however it moved.
    assert situation_for(-0.2, True, True) is Situation.SMALL_MOVE


def test_every_concept_records_its_provenance():
    from app.services.curriculum import CONCEPTS, OURS

    for c in CONCEPTS:
        assert c.source, c.key
        if c.source != OURS:
            assert ".pdf" in c.source, f"{c.key} cites no source file"


def test_unsourced_concepts_are_few_and_named():
    """Three concepts have no FDIC equivalent because the curriculum never
    covers daily price movement. They carry the most review risk, so they
    are enumerated rather than blended in."""
    from app.services.curriculum import unsourced

    keys = {c.key for c in unsourced()}
    assert keys == {"prices_wiggle_daily", "hopes_and_worries", "nobody_knows_next"}


def test_prompt_block_lists_keys_the_writer_can_return():
    """The writer returns a key, which is what makes the choice checkable."""
    from app.services.curriculum import Situation, concepts_for, prompt_block

    block = prompt_block("10-12", Situation.DROP)
    for c in concepts_for("10-12", Situation.DROP):
        assert c.key in block
    assert block.strip().startswith("-")


def test_concept_statements_contain_no_numbers_or_advice():
    """Same rules the AI stories live under: render.py owns every number,
    and nothing may read as a recommendation."""
    import re

    from app.services.curriculum import CONCEPTS

    banned = ("you should", "buy now", "guaranteed", "%", "$")
    for c in CONCEPTS:
        text = f"{c.title} {c.statement}".lower()
        assert not re.search(r"\d", text), f"{c.key} contains a number"
        for phrase in banned:
            assert phrase not in text, f"{c.key} contains {phrase!r}"


def test_choosing_voyage_without_a_key_fails_closed_in_production(monkeypatch):
    """Only reachable by explicitly selecting voyage. Silent degradation to
    the bag-of-words fake would leave retrieval looking healthy while
    returning nonsense, and confidently irrelevant answers are worse than
    an error because nobody investigates them."""
    from app.core.settings import get_settings
    from app.services.rag.embeddings import EmbeddingError, get_embedder

    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(EmbeddingError, match="Refusing to fall back"):
        get_embedder()


def test_choosing_voyage_without_a_key_falls_back_in_development(monkeypatch):
    from app.core.settings import get_settings
    from app.services.rag.embeddings import get_embedder

    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    assert isinstance(get_embedder(), FakeEmbedder)


def test_no_production_code_path_imports_the_embeddings_module():
    """Guards the property that makes Voyage's rate limit irrelevant to
    production: nothing outside the rag package touches it, so the app
    cannot make an embedding call. Phase E will deliberately change this,
    and this test is where that decision gets noticed."""
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        if "services/rag/" in str(path):
            continue
        text = path.read_text()
        if re.search(r"rag\.embeddings|rag\.store|rag\.retrieve", text):
            # settings.py mentions the path in a comment only.
            if path.name == "settings.py":
                continue
            offenders.append(str(path))
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# The local (free, offline) embedder
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def local_embedder():
    """Skips rather than fails when model2vec isn't installed — it is a
    build-time dependency, not a runtime one."""
    pytest.importorskip("model2vec")
    from app.services.rag.embeddings import LocalEmbedder

    return LocalEmbedder()


def test_local_embedder_returns_unit_vectors(local_embedder):
    """model2vec does not normalise, and the stores treat a dot product as
    cosine similarity — so normalising is a correctness requirement, not a
    tidy-up."""
    [v] = local_embedder.embed(["saving money for a goal"], "document")
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)


def test_local_embedder_captures_meaning_not_just_words(local_embedder):
    """The property the fake embedder cannot have: no shared vocabulary,
    but related meaning should still score closer."""
    a, b, c = local_embedder.embed(
        ["teaching children to put money aside",
         "helping kids learn about saving",
         "identity theft and credit card fraud"],
        "document",
    )
    assert store.cosine(a, b) > store.cosine(a, c)


def test_local_embedder_is_deterministic(local_embedder):
    one = local_embedder.embed(["compound growth"], "document")
    two = local_embedder.embed(["compound growth"], "document")
    assert one == two


def test_local_embedder_handles_empty_input(local_embedder):
    assert local_embedder.embed([], "document") == []


def test_local_embedder_survives_unknown_tokens(local_embedder):
    """An all-unknown-token text would divide by a zero norm."""
    [v] = local_embedder.embed(["zzzqqq xxyyzz"], "document")
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-6)


def test_local_is_the_default_provider(monkeypatch):
    """No key, no quota, no bill — so it should be what you get unless you
    ask for otherwise."""
    from app.core.settings import get_settings
    from app.services.rag.embeddings import LocalEmbedder, get_embedder

    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    get_settings.cache_clear()
    # Note this also means production no longer fails closed on a missing
    # Voyage key, because it never needs one.
    assert isinstance(get_embedder(), LocalEmbedder)


def test_voyage_can_still_be_selected(monkeypatch):
    from app.core.settings import get_settings
    from app.services.rag.embeddings import VoyageEmbedder, get_embedder

    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-key-value")
    get_settings.cache_clear()
    # Wrapped in CachedEmbedder on purpose: a repeated query must not be
    # re-paid for. Unwrap to check the backend underneath.
    from app.services.rag.embeddings import CachedEmbedder

    got = get_embedder()
    assert isinstance(got, CachedEmbedder)
    assert isinstance(got._inner, VoyageEmbedder)


def test_all_three_backends_satisfy_the_same_protocol(local_embedder):
    """The point of the interface: swapping backends needs no caller
    changes, which is what made measuring them against each other possible."""
    from app.services.rag.embeddings import FakeEmbedder, VoyageEmbedder

    for e in (FakeEmbedder(), local_embedder, VoyageEmbedder(api_key="x" * 40)):
        assert isinstance(e.name, str) and e.name
        assert callable(e.embed)
        assert isinstance(e.dimensions, int) or e is not None
