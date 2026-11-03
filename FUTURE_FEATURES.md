# Future Features

Ideas for future development of Little Money Monkey.

---

# 📖 READ FIRST — Understanding the RAG implementation

**Status as of 2026-11-02:** the pipeline is built, tested (76 tests) and
measured, and it reaches **no user yet**. `app/services/rag/` is imported
only by tests; `app/services/curriculum.py` is imported by nothing at all.
Committed in `953a805`.

This note exists because the code is the teaching material. Every module
docstring explains *why* the decision was made that way, not just what the
code does. Read the docstrings, not the function bodies.

## Read our own code in this order

Each one takes 10-15 minutes. Do them in order — each builds on the last.

| # | File | The idea it teaches |
| - | ---- | ------------------- |
| 1 | `app/services/rag/corpus.py` | **Deciding what NOT to index.** 17 of 29 PDFs selected, 12 excluded with a written reason each. Indexing everything measurably hurt us. |
| 2 | `app/services/rag/chunking.py` | **Chunking is the decision that determines quality.** Splitting on the document's real structure instead of every N characters. Also: why the age band comes from the filename and never the document text. |
| 3 | `app/services/rag/embeddings.py` | **What an embedding is**, query/document asymmetry, why vectors must be unit length, and why the test fake hashes words instead of being random. |
| 4 | `app/services/rag/store.py` | **Cosine similarity by hand.** Written out rather than imported, so the maths is visible. Also why we need no vector database at this size. |
| 5 | `app/services/rag/retrieve.py` | **Reciprocal Rank Fusion**, and why hybrid search is turned OFF here against the usual advice. |
| 6 | `app/services/rag/evaluate.py` | **precision@k / recall@k / MRR.** The part most projects skip. Read this one twice. |

Then `app/services/curriculum.py` for how retrieval is meant to feed the
story prompts — the step that is still unwired.

## The one-paragraph version

A RAG system answers a question by **finding relevant text first and
putting it in the prompt**, instead of hoping the model already knows. Five
stages: pick the documents (`corpus`), cut them into retrievable pieces
(`chunking`), turn each piece into a vector that encodes its meaning
(`embeddings`), store the vectors and find the nearest ones to a question
(`store`, `retrieve`), and — the part that makes it engineering rather than
guesswork — **measure whether the pieces it found were the right ones**
(`evaluate`).

## Where to learn the concepts properly

Our code assumes you already know roughly what a vector is. If not, start
with the first two.

- **Embeddings, from scratch** — Cohere's *LLM University*, "Text
  Embeddings" chapter. Free, visual, no maths background needed. The best
  starting point for "what IS a vector, really".
- **Anthropic's embeddings + RAG docs** — <https://docs.claude.com>, search
  "embeddings" and "contextual retrieval". Short, practical, and matches
  the provider we actually use. The *Contextual Retrieval* post is the
  single highest-value read for improving what we have.
- **Pinecone's learning centre** — <https://www.pinecone.io/learn/>
  Vendor-written but genuinely good on chunking strategies, hybrid search
  and RRF. Read it for the concepts; ignore the "you need Pinecone" part,
  because at 149 chunks we demonstrably do not.
- **The RRF paper** — Cormack et al. (2009), "Reciprocal Rank Fusion
  outperforms Condorcet". Six pages. Worth reading because our measurements
  **contradict** its conclusion on this corpus, and understanding why is
  the most interesting thing in this project.
- **"Evaluating RAG"** — search for RAGAS and the precision/recall/MRR
  trio. Treat library choice as secondary; the metrics are the point, and
  `evaluate.py` implements them in ~200 lines so nothing is hidden.

**Skip for now:** LangChain and LlamaIndex tutorials. They would have
hidden every decision documented above behind a one-line call, and the
decisions are where the quality came from.

## What is distinctive about THIS project

If you are explaining this work to someone — an interviewer, a reviewer —
these four points are the content. Everything else is plumbing.

1. **Retrieval quality is a number, not an opinion.** 8 golden queries,
   precision@k / recall@k / MRR, re-runnable. Most RAG projects have no
   evaluation at all, so every later decision is a guess.

2. **A measurement overturned the conventional design.** Hybrid search is
   standard advice; here it was worse in all three configurations tested
   (P@5 0.950 → 0.700 with Voyage, 0.800 → 0.625 and 0.675 → 0.550
   locally). It is OFF, and `retrieve.py` records the exact conditions
   under which that should be revisited. Knowing *why* — RRF weights both
   arms equally, so a weak keyword arm drags down a strong vector arm — is
   the part worth being able to explain.

3. **Cost was a design constraint, measured rather than assumed.** Voyage
   scored ~15 points of precision better but costs money and has a quota
   that stalled development. model2vec runs locally for $0, needs no
   network, and installs on Python 3.14 where torch does not. Embedding is
   a build-time step, so production pays nothing either way. The right
   answer depended on knowing the size of the gap.

4. **Correctness was separated from ranking.** Serving grade 9-12 material
   for a 4-year-old is not a bad ranking, it is a bug — so age-group
   violations are counted separately and sit at 0. Folding them into
   precision would have let good ranking hide them.

Two honest caveats to state rather than hide: the golden set is only 8
queries (thin for literal-heavy questions — just 2 of 8), and relevance is
judged by source section rather than per-chunk labels, which is cheap to
maintain but coarse.

## What is still pending

- **Ranking and rotation**, then wire `curriculum.prompt_block()` into
  `app/services/articles/prompts.py`. Until this lands, none of the above
  affects a single email. A 10-12 year old on a down day currently matches
  ~18 of 21 concepts; handing a model 18 options is noise, not grounding.
- 3 concepts are still unsourced — `curriculum.unsourced()` lists them.
- `model2vec` and `pypdf` are declared in no requirements file; they work
  only because they are in the local venv. `model2vec` must NOT go in
  `requirements.txt` (Railway would install ~30MB it never uses) — it needs
  a `requirements-dev.txt`.
- `RAG_*` and `VOYAGE_API_KEY` are undocumented in `.env.example` and
  `DEPLOYMENT.md`. Document them when the wiring lands, not before.
- `store.py::PgVectorStore` is written but never tested against a real
  Postgres, and `CREATE EXTENSION vector` may not be available on Railway's
  image. At 149 chunks (~150KB of vectors) it is not needed — do not test
  it until it is.

## Earlier notes (kept)

RAG source material: <https://www.fdic.gov/consumer-resource-center/teach-money-smart>
— public domain, which is why it can be committed to this repo. The 29 PDFs
are in `backend/RAG_sources/`.

If the goal is to train a model on this data later, keeping it here is still
the right choice. There is currently no clean way to migrate off Railway.

---
## 🐵 Interactive Kid Conversations (Money Monkey Chat)

**Vision:** After a parent reads a Money Idea to their child, the child can
"talk back" through the app. The child answers a question or explains what
they learned, and our AI responds with encouragement, follow-up questions
and deeper explanations — all at the child's age level.

### How it would work

1. **Parent receives a Money Idea** in the daily recap (as today).
2. **Parent opens the Money Ideas page** and taps "Talk about this one."
3. **Child types or dictates** their answer to the question (e.g. "I think
   a company is like my art club because we make stuff and sell it!").
4. **AI responds** with encouragement and a follow-up, at the child's age
   level: "That's a great way to think about it! Your art club is a lot
   like a company. What would happen if lots of people wanted to buy your
   art?"
5. **Conversation is saved** so the parent can review it later and see
   what their child understood.

### Technical approach

- New `money_idea_conversations` table: `(id, user_id, idea_key, messages
  JSONB, created_at, updated_at)`.
- Messages are `[{role: "child" | "ai", text, timestamp}]`.
- AI responses use the same provider (Claude Haiku) with a system prompt
  tuned for the child's age group, the specific money idea, and positive
  reinforcement.
- **Safety:** All AI responses go through the same language checks as
  stock stories. The AI never gives advice, never uses scary language,
  and always encourages curiosity. Parent can view all conversations.
- **Rate limit:** Max 5 exchanges per idea per day (prevents cost
  runaway).
- **Cost estimate:** ~$0.005 per exchange (Haiku input + output), so ~10
  exchanges/day = ~$0.05/user/day, ~$1.50/user/month at daily use.

### Prerequisites

- Enough users to validate the idea (target: 20+ active families).
- Parent feedback on which Money Ideas spark the most conversation.
- Collected examples of real kid responses (opt-in) to tune the AI prompt.

---

## 📊 Financial Analogy History & Progress Tracking

**Vision:** Every financial analogy sent to a user is stored, creating a
"learning journal" the family can revisit. Over time, parents can see
their child's growing financial vocabulary.

### Already implemented (v1)

- `MoneyIdeaSend` records every idea sent (user, idea_key, trading_date).
- `/money-ideas` page shows the full history.
- 20-idea curriculum with basics-first ordering.

### Future additions

- **Comprehension tracking:** After the interactive conversation feature,
  track which ideas the child understood well vs. struggled with.
- **Spaced repetition:** Re-introduce ideas the child found tricky after
  a gap, using the conversation feedback as signal.
- **Progress badges:** "You've learned 10 ideas!" celebrations in the
  recap email.
- **Family sharing:** Let both parents see the same history (shared
  household accounts).

---

## 🤖 Custom AI Model (When Ready)

**Current state:** AI stories use Claude Haiku ($0.026/story). The Money
Idea curriculum is hand-written (free). Both work well for the current
scale.

**When to consider fine-tuning:**

| Signal | Threshold |
|--------|-----------|
| Active families | 50+ |
| Conversation data points | 500+ kid responses |
| Parent feedback entries | 100+ "this landed" / "this didn't" |

**Options when the time comes:**

| Approach | Cost | Pros | Cons |
|----------|------|------|------|
| Better prompts (current) | $0 | Iterate fast, no data needed | Limited by prompt length |
| Fine-tune open model (LoRA, 7-8B) | $50-300/run + $50-150/mo hosting | Full control, own data | Need training data, hosting |
| Fine-tune hosted model (Anthropic/OpenAI) | $0.008-0.016/1K tokens training | No hosting, easy API | Less control, vendor lock-in |
| Train from scratch | $10K+ | Total control | Impractical at this scale |

**Recommended path:** Collect parent feedback and kid conversations first.
When there are 500+ data points of "this analogy worked for age X" vs.
"this one didn't land," fine-tune a LoRA adapter on an open 7-8B model.
Until then, prompt engineering and curated curriculum content deliver
better results per dollar.

---

## 👍 Parent Feedback (Like / So-so / Dislike)

**Vision:** Every AI story and every Money Idea sent to a parent includes
three quick-tap feedback buttons. Parents rate content in one click, and
the data feeds every downstream improvement — prompt tuning, curriculum
rewrites, RAG retrieval and (eventually) fine-tuning.

### How it would work

1. **In emails:** Three links at the bottom of each AI story and each
   Money Idea block — 👍 Love it, 😐 So-so, 👎 Not great. Each link hits
   a signed, one-click API endpoint (no login required).
2. **On the frontend:** `/money-ideas` page shows the user's past ratings
   and lets them change one. AI story feedback shows on a future
   `/stories` page.
3. **Data stored:** `ContentFeedback(user_id, content_type, content_id,
   rating, created_at)`. `content_type` is `"article"` (StockArticle id)
   or `"money_idea"` (idea_key + trading_date). `rating` is `"like"`,
   `"neutral"`, `"dislike"`.
4. **Admin visibility:** Daily summary includes feedback counts. A future
   admin dashboard shows trends (which ideas land, which AI stories
   resonate, per age group).

### Why this unblocks other features

| Downstream feature | Needs feedback? |
|--------------------|-----------------|
| Prompt tuning | Yes — know which tones/analogies work |
| Curriculum rewrites | Yes — know which ideas are too hard or too dry |
| RAG retrieval (see below) | Yes — only retrieve highly-rated examples |
| Fine-tuning | Yes — need labeled training data |
| Gamification / streaks | Nice to have — engaged parents rate more |

### Cost & complexity

Zero AI cost. One new DB table, one new API route, three links in email
templates. Estimated effort: 1 day.

---

## 🧠 RAG from Highly-Rated Content

**Vision:** Once enough parents have rated AI stories and Money Ideas,
build a retrieval-augmented generation (RAG) layer so the AI can see
"stories that families loved" before writing new ones.

### How it would work

1. **Collect ratings** (from the feedback feature above). Wait for 100+
   rated stories before building the first index.
2. **Build an embedding index** of highly-rated stories (rating = "like"):
   embed each story's text (age group + content) with a small embedding
   model (e.g. `text-embedding-3-small` at $0.02/M tokens).
3. **At generation time:** before writing a new story, retrieve the 2-3
   most similar highly-rated stories for the same age group and include
   them as few-shot examples in the prompt ("Here are stories that
   families loved — match their tone and approach").
4. **Periodic refresh:** re-index weekly as new ratings come in.

### Why this is better than fine-tuning (for now)

| Approach | Data needed | Cost | Iteration speed |
|----------|-------------|------|-----------------|
| RAG (few-shot retrieval) | 100+ rated stories | ~$0.001/query (embedding) | Instant — new stories enter index immediately |
| LoRA fine-tune | 500+ labeled examples | $50-300/run + hosting | Days — retrain on new data |
| Full fine-tune | 1000+ examples | $500+ | Weeks |

RAG gives 80% of the benefit at 1% of the cost and can start much sooner.
When there are 500+ data points, fine-tuning can further improve results
on top of RAG.

### Prerequisites

- Parent feedback feature live and collecting data.
- 100+ "like" ratings on AI stories.
- Vector store (pgvector extension on Railway Postgres, or a lightweight
  FAISS index serialized to disk).

---

## 💰 AI Story Caching (Smart Reuse)

**Vision:** When today's stock move is very similar to a recent move and
the news context is the same, reuse the research or the full story instead
of paying for a new one.

### Current architecture (already efficient)

- `StockArticle` is **shared across all users** for the same (ticker,
  trading_date, direction). One story per stock per day, no matter how
  many users get the alert. This is the biggest cost saver already.
- Small moves (< 1% stock, < 0.5% fund) skip AI entirely and get the
  fixed "small move" note. "Moved with the market" gets a fixed note too.

### What caching would add

| Layer | Saves | Risk | Complexity |
|-------|-------|------|------------|
| **Research cache**: reuse web search results for the same ticker within the same day | ~$0.02/story (2 web searches) | Low — news doesn't change within hours | Low — key by (ticker, date) |
| **Cross-day story reuse**: if AAPL dropped 2% yesterday for the same reason, reuse the story | ~$0.026/story | **High** — market context differs; stale stories mislead | High — similarity matching, staleness detection |
| **Embedding-based dedup**: embed the news articles; if cosine similarity > 0.95, reuse | ~$0.025/story | Medium — need a threshold that avoids false positives | Medium — embedding + comparison |

### Recommendation

**Same-day research caching** is worth doing now — it's safe and simple.
The `StockArticle` sharing already handles the main case; research
caching adds savings when the same ticker triggers multiple intraday runs
on the same day.

**Cross-day reuse** is risky and should wait until there's data showing
it's needed (e.g., 50+ users, 100+ stories/day). Even then, a
conservative approach — only reuse when the top news article is identical
— is safer than similarity matching.

**Current cost at scale:** At 30 stories/day (the cap), daily AI cost is
~$0.78. At 100 users, most stories are shared, so cost scales with
*tickers watched* not *users*. Caching saves meaningful money only above
~200 stories/day.

---

## 🎮 Gamification

- **Daily streaks:** "You've talked about money 5 days in a row!"
- **Idea badges:** Visual badges for completing curriculum sections
  (Basics, How Markets Work, Smart Money Habits).
- **Family leaderboard:** Optional, showing which families talked the
  most this week (opt-in, privacy-first).

---

## 📱 Mobile App

- Push notifications for recaps and story follow-ups.
- Kid-friendly UI mode with larger text and fun illustrations.
- Voice input for younger children's responses.

---

## 🌍 Internationalization

- Multi-language curriculum (Spanish, Mandarin, etc.).
- Regional market support (London Stock Exchange, Tokyo, etc.).
- Currency-appropriate analogies.
