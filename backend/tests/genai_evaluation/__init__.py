"""Gen AI Evaluation Suite

AI-powered evaluation of generated content quality, age-appropriateness,
and safety for kids. These tests use an LLM-as-judge pattern to assess
whether AI-generated stock stories and curriculum content meet the
standards a parent would expect.

Run with: pytest tests/genai_evaluation/ -v
Requires: ANTHROPIC_API_KEY or GEMINI_API_KEY in environment.
Skipped automatically when no AI key is available.
"""
