"""Turn a stored story into the email block (plain text + HTML).

Everything that could mislead comes from our code, not the AI: the numbers,
the mood emoji, the 30-day trend, the links (re-checked against the allowed
news sites here as a second line of defence) and the disclaimer. All
AI-written text is HTML-escaped, so a story can never inject markup or
links into an email.

Shows the age-group versions the parent chose; the numbers, grown-up part,
sources and disclaimer appear once.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from app.services.articles.schemas import AGE_GROUPS, Fact, KidVersion, Story
from app.services.articles.validate import is_allowed_url

# Shown on every story. Kept in code (not written by the AI) so it's always
# present and always worded the same.
DISCLAIMER = (
    "About this story: it was written by AI from the news linked above and "
    "has not been checked by a person. AI can make mistakes, leave things "
    "out, or reflect bias. Please read it yourself before sharing it with "
    "your child, and check the sources for the details. It's for learning "
    "and family conversation only: it is not financial, investment, tax or "
    "legal advice, and not a recommendation to buy, sell or hold any "
    "investment. Any decisions you make are your own. Prices may be delayed."
)
AGE_LABELS = {
    "3-5": "🧸 For your little one (ages 3–5)",
    "6-9": "🎒 For your kid (ages 6–9)",
    "10-12": "🧠 For your tween (ages 10–12)",
    "13-17": "🎧 For your teen (ages 13–17)",
}
_SPARK = "▁▂▃▄▅▆▇█"
_UP, _DOWN, _MUTED = "#1a7f37", "#cf222e", "#6e7781"


@dataclass
class StoryView:
    symbol: str
    company: str
    move_pct: float
    market_pct: float | None
    as_of: str  # e.g. "12:00 PM ET"
    story: Story
    facts: list[Fact]
    closes: list[float]  # recent daily closes, oldest first, ending with today's price
    allowed_domains: list[str]
    age_groups: list[str]  # the parent's choice


def mood_emoji(move_pct: float, tone: str) -> str:
    if tone == "gentle":
        return "📌"
    if move_pct >= 8:
        return "🚀"
    if move_pct >= 3:
        return "📈"
    if move_pct <= -8:
        return "🎢"
    if move_pct <= -3:
        return "📉"
    return "😐"


def sparkline(values: list[float]) -> str:
    """Tiny unicode chart, readable in every mail app and in plain text."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK[3] * len(values)
    step = (hi - lo) / (len(_SPARK) - 1)
    return "".join(_SPARK[round((v - lo) / step)] for v in values)


def versions_for(story: Story, age_groups: list[str]) -> list[KidVersion]:
    """The chosen groups' versions. If none of them passed the safety checks,
    the closest available age instead, so the parent still gets a story."""
    chosen = [v for g in age_groups if (v := story.version(g)) is not None]
    if chosen or not story.versions:
        return chosen
    target = AGE_GROUPS.index(age_groups[0]) if age_groups and age_groups[0] in AGE_GROUPS else 1
    return [min(story.versions, key=lambda v: abs(AGE_GROUPS.index(v.age_group) - target))]


def _links(v: StoryView) -> list[tuple[str, str]]:
    """(site name, url) per source, allowed sites only, no duplicates."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for f in v.facts:
        if f.source_url in seen or not is_allowed_url(f.source_url, v.allowed_domains):
            continue
        seen.add(f.source_url)
        out.append((f.source_name or "Source", f.source_url))
    return out


def _numbers_line(v: StoryView) -> str:
    line = f"{v.symbol} {v.move_pct:+.2f}% today"
    if v.market_pct is not None:
        line += f" · S&P 500 {v.market_pct:+.2f}%"
    return line


def story_text(v: StoryView) -> str:
    s = v.story
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "📰 STOCK STORY: read it together with your kids",
        _numbers_line(v),
    ]
    trend = sparkline(v.closes)
    if trend:
        lines.append(f"Last {len(v.closes)} days: {trend}")
    for kid in versions_for(s, v.age_groups):
        lines += [
            "",
            AGE_LABELS[kid.age_group],
            f"{mood_emoji(v.move_pct, s.tone)} {kid.headline}",
            kid.story,
            f"💡 Money idea: {kid.lesson_title}. {kid.lesson}",
            f"🗣️ Ask your kid: {kid.talk_together}",
        ]
    lines += ["", "🧐 For grown-ups", s.grown_up_summary]
    for f in v.facts:
        lines.append(f'"{f.quote}" ({f.source_name})')
    links = _links(v)
    if links:
        lines.append("Read more:")
        lines += [f"  {name}: {url}" for name, url in links]
    lines += [
        f"Story as of {v.as_of}. Prices keep moving; the sources have the latest news.",
        "",
        f"⚠️ {DISCLAIMER}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def _bar(label: str, pct: float, color: str) -> str:
    # 10% fills the bar; table cells survive every mail client's CSS stripping.
    width = max(2, min(100, round(abs(pct) * 10)))
    return (
        '<tr><td style="padding:2px 8px 2px 0;font-size:13px;white-space:nowrap">'
        f"{escape(label)}</td>"
        '<td style="width:100%;padding:2px 0"><table role="presentation" '
        f'cellpadding="0" cellspacing="0" style="width:{width}%"><tr>'
        f'<td style="background:{color};height:10px;border-radius:3px"></td>'
        "</tr></table></td>"
        f'<td style="padding:2px 0 2px 8px;font-size:13px;white-space:nowrap">'
        f"{pct:+.2f}%</td></tr>"
    )


def _kid_html(kid: KidVersion, emoji: str) -> str:
    return f"""\
<div style="border-top:1px solid #eaeef2;margin-top:14px;padding-top:10px">
<p style="margin:0;font-size:13px;color:{_MUTED}">{escape(AGE_LABELS[kid.age_group])}</p>
<h2 style="margin:4px 0 8px;font-size:19px">{emoji} {escape(kid.headline)}</h2>
<p style="font-size:16px;line-height:1.5;margin:0 0 8px">{escape(kid.story)}</p>
<p style="margin:0 0 8px"><strong>💡 Money idea: {escape(kid.lesson_title)}.</strong> {escape(kid.lesson)}</p>
<p style="background:#fff8c5;border-radius:8px;padding:10px;margin:0">🗣️ <strong>Ask your kid:</strong> {escape(kid.talk_together)}</p>
</div>"""


def story_html(v: StoryView) -> str:
    s = v.story
    color = _UP if v.move_pct >= 0 else _DOWN
    bars = _bar(v.symbol, v.move_pct, color)
    if v.market_pct is not None:
        bars += _bar("S&P 500", v.market_pct, _MUTED)
    trend = sparkline(v.closes)
    trend_html = (
        f'<p style="margin:4px 0 0;font-size:13px;color:{_MUTED}">Last '
        f'{len(v.closes)} days: <span style="font-size:16px;color:{color}">'
        f"{trend}</span></p>"
        if trend else ""
    )
    emoji = mood_emoji(v.move_pct, s.tone)
    kids = "".join(_kid_html(k, emoji) for k in versions_for(s, v.age_groups))
    quotes = "".join(
        f'<p style="margin:6px 0;font-style:italic">“{escape(f.quote)}” '
        f'<span style="font-style:normal;color:{_MUTED}">({escape(f.source_name)})</span></p>'
        for f in v.facts
    )
    links = " · ".join(
        f'<a href="{escape(url, quote=True)}">{escape(name)} ↗</a>' for name, url in _links(v)
    )
    return f"""\
<div style="border:1px solid #d0d7de;border-radius:10px;padding:16px;margin:16px 0">
<p style="margin:0 0 8px;font-size:12px;letter-spacing:.04em;color:{_MUTED}">📰 STOCK STORY · READ IT TOGETHER WITH YOUR KIDS</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%">{bars}</table>
{trend_html}
{kids}
<div style="border-top:1px solid #eaeef2;margin-top:14px;padding-top:10px">
<p style="margin:0 0 2px"><strong>🧐 For grown-ups</strong></p>
<p style="margin:0">{escape(s.grown_up_summary)}</p>
{quotes}
<p style="margin:6px 0">{'Read more: ' + links if links else ''}</p>
<p style="font-size:12px;color:{_MUTED};margin:4px 0 0">Story as of {escape(v.as_of)}. Prices keep moving; the sources have the latest news.</p>
</div>
<p style="font-size:12px;line-height:1.45;color:#57606a;background:#f6f8fa;border-radius:8px;padding:10px;margin:12px 0 0">⚠️ {DISCLAIMER}</p>
</div>"""


def still_looking_text(symbol: str) -> str:
    return (
        f"🔎 We're still looking into why {symbol} moved. If we find the story, "
        "we'll email it to you in the next few minutes."
    )


def email_html(intro: str, block_html: str, manage_url: str, unsubscribe_url: str) -> str:
    """Whole HTML email around a story block (or a short note)."""
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:560px;color:#1f2328">
<p style="font-size:15px">{escape(intro)}</p>
{block_html}
<p style="font-size:12px;color:{_MUTED}">You're receiving this because you set a price-change rule for this stock.<br>
<a href="{escape(manage_url, quote=True)}">Manage your watchlist, rules and story ages</a> · <a href="{escape(unsubscribe_url, quote=True)}">Stop all alert emails</a></p>
</div>"""
