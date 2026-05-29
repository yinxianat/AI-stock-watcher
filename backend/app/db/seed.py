"""Seed the ticker catalog with popular ETFs and stocks.

Run from project root:  python -m app.db.seed
Idempotent: safe to re-run; existing symbols are left untouched.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.database import get_engine, get_session_factory
from app.models import Base, Ticker, TickerType

ETFS: list[tuple[str, str]] = [
    ("SPY", "SPDR S&P 500 ETF Trust"),
    ("VOO", "Vanguard S&P 500 ETF"),
    ("IVV", "iShares Core S&P 500 ETF"),
    ("QQQ", "Invesco QQQ Trust (Nasdaq-100)"),
    ("VTI", "Vanguard Total Stock Market ETF"),
    ("VXUS", "Vanguard Total International Stock ETF"),
    ("VT", "Vanguard Total World Stock ETF"),
    ("DIA", "SPDR Dow Jones Industrial Average ETF"),
    ("IWM", "iShares Russell 2000 ETF"),
    ("VEA", "Vanguard FTSE Developed Markets ETF"),
    ("VWO", "Vanguard FTSE Emerging Markets ETF"),
    ("VNQ", "Vanguard Real Estate ETF"),
    ("VGT", "Vanguard Information Technology ETF"),
    ("VHT", "Vanguard Health Care ETF"),
    ("VFH", "Vanguard Financials ETF"),
    ("VDE", "Vanguard Energy ETF"),
    ("VPU", "Vanguard Utilities ETF"),
    ("VCR", "Vanguard Consumer Discretionary ETF"),
    ("VDC", "Vanguard Consumer Staples ETF"),
    ("VAW", "Vanguard Materials ETF"),
    ("VIS", "Vanguard Industrials ETF"),
    ("VOX", "Vanguard Communication Services ETF"),
    ("AGG", "iShares Core U.S. Aggregate Bond ETF"),
    ("BND", "Vanguard Total Bond Market ETF"),
    ("BNDX", "Vanguard Total International Bond ETF"),
    ("VCIT", "Vanguard Intermediate-Term Corporate Bond ETF"),
    ("VCSH", "Vanguard Short-Term Corporate Bond ETF"),
    ("VGSH", "Vanguard Short-Term Treasury ETF"),
    ("VGIT", "Vanguard Intermediate-Term Treasury ETF"),
    ("VGLT", "Vanguard Long-Term Treasury ETF"),
    ("GLD", "SPDR Gold Shares"),
    ("XLK", "Technology Select Sector SPDR"),
    ("XLF", "Financial Select Sector SPDR"),
]

STOCKS: list[tuple[str, str]] = [
    ("AAPL", "Apple Inc."),
    ("MSFT", "Microsoft Corporation"),
    ("GOOGL", "Alphabet Inc. (Class A)"),
    ("GOOG", "Alphabet Inc. (Class C)"),
    ("AMZN", "Amazon.com, Inc."),
    ("META", "Meta Platforms, Inc."),
    ("NVDA", "NVIDIA Corporation"),
    ("TSLA", "Tesla, Inc."),
    ("BRK-B", "Berkshire Hathaway Inc. (Class B)"),
    ("JPM", "JPMorgan Chase & Co."),
    ("V", "Visa Inc."),
    ("MA", "Mastercard Incorporated"),
    ("UNH", "UnitedHealth Group Incorporated"),
    ("XOM", "Exxon Mobil Corporation"),
    ("CVX", "Chevron Corporation"),
    ("WMT", "Walmart Inc."),
    ("PG", "Procter & Gamble Co."),
    ("KO", "The Coca-Cola Company"),
    ("PEP", "PepsiCo, Inc."),
    ("MCD", "McDonald's Corporation"),
    ("DIS", "The Walt Disney Company"),
    ("NFLX", "Netflix, Inc."),
    ("ADBE", "Adobe Inc."),
    ("CRM", "Salesforce, Inc."),
    ("ORCL", "Oracle Corporation"),
    ("INTC", "Intel Corporation"),
    ("AMD", "Advanced Micro Devices, Inc."),
    ("CSCO", "Cisco Systems, Inc."),
    ("PFE", "Pfizer Inc."),
    ("JNJ", "Johnson & Johnson"),
    ("LLY", "Eli Lilly and Company"),
    ("HD", "The Home Depot, Inc."),
    ("COST", "Costco Wholesale Corporation"),
    ("NKE", "NIKE, Inc."),
    ("BAC", "Bank of America Corporation"),
    ("T", "AT&T Inc."),
    ("VZ", "Verizon Communications Inc."),
    ("BA", "The Boeing Company"),
    ("UBER", "Uber Technologies, Inc."),
    ("SHOP", "Shopify Inc."),
]


def seed() -> int:
    """Insert any missing seed tickers. Returns # of new rows."""
    Base.metadata.create_all(bind=get_engine())
    db = get_session_factory()()
    inserted = 0
    try:
        existing = {
            s for (s,) in db.execute(select(Ticker.symbol)).all()
        }
        for sym, name in ETFS:
            if sym in existing:
                continue
            db.add(Ticker(symbol=sym, name=name, type=TickerType.ETF, is_seeded=True))
            inserted += 1
        for sym, name in STOCKS:
            if sym in existing:
                continue
            db.add(Ticker(symbol=sym, name=name, type=TickerType.STOCK, is_seeded=True))
            inserted += 1
        db.commit()
    finally:
        db.close()
    return inserted


if __name__ == "__main__":  # pragma: no cover
    n = seed()
    print(f"Seeded {n} new tickers.")
