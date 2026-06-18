"""Seed the ticker catalog with popular ETFs and stocks, and populate a
demo watchlist for the first user so the intraday capture job has tickers
to process immediately after initialisation.

Run from project root:  python -m app.db.seed
Idempotent: safe to re-run; called automatically from `lifespan()` on every
app startup (and therefore on every production redeploy on Railway).

Guarantees:

* Existing tickers that match a seed entry are UPDATED only on `name`/`type`
  drift, and only if we own them (`is_seeded=True`). Tickers the user added
  themselves (`is_seeded=False`) are never touched.
* No row is ever deleted by seed — symbols dropped from the catalog stay in
  the DB so watchlist FK references remain valid.
* The demo watchlist items (DEMO_WATCHLIST symbols) are added to the first
  user's watchlist only if that user exists and the item is not already
  present — existing watchlist entries are never modified or removed.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.database import get_engine, get_session_factory
from app.models import Base, Ticker, TickerType, User, WatchlistItem

log = logging.getLogger(__name__)

# Symbols added to the first user's watchlist on every startup so the
# intraday capture job has something to fetch immediately.
DEMO_WATCHLIST: list[str] = ["SPY", "QQQ", "AAPL", "MSFT"]

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


def _seed_demo_watchlist(db) -> int:
    """Add DEMO_WATCHLIST tickers to the first user's watchlist.

    Idempotent: skips any (user_id, ticker_id) pair that already exists.
    Returns the number of WatchlistItem rows inserted.
    """
    first_user: User | None = db.execute(
        select(User).order_by(User.id).limit(1)
    ).scalar_one_or_none()
    if first_user is None:
        log.info("Seed watchlist: no users in DB yet — skipping demo watchlist")
        return 0

    # Fetch the ticker rows for the demo symbols in one query.
    tickers: list[Ticker] = db.execute(
        select(Ticker).where(Ticker.symbol.in_(DEMO_WATCHLIST))
    ).scalars().all()
    if not tickers:
        log.warning("Seed watchlist: none of %s found in tickers table", DEMO_WATCHLIST)
        return 0

    # Build a set of ticker_ids already on this user's watchlist.
    existing_ids: set[int] = {
        tid for (tid,) in db.execute(
            select(WatchlistItem.ticker_id).where(
                WatchlistItem.user_id == first_user.id
            )
        ).all()
    }

    inserted = 0
    for ticker in tickers:
        if ticker.id in existing_ids:
            continue
        db.add(WatchlistItem(user_id=first_user.id, ticker_id=ticker.id))
        inserted += 1

    if inserted:
        log.info(
            "Seed watchlist: added %d demo ticker(s) to user %d (%s)",
            inserted, first_user.id, first_user.email,
        )
    else:
        log.info(
            "Seed watchlist: demo tickers already present for user %d — nothing to add",
            first_user.id,
        )
    return inserted


def seed() -> int:
    """Sync the ticker catalog with the in-code seed list. Returns # inserted.

    Behavior on each call (every app startup, including prod redeploys):

    * Brand-new seed symbols are inserted with `is_seeded=True`.
    * Existing tickers we own (`is_seeded=True`) get name/type updates if the
      catalog has drifted — e.g. you edit "Apple Inc." to add Inc.
    * Existing tickers a user added themselves (`is_seeded=False`) are NEVER
      modified, even if they happen to share a symbol with the seed list.
    * Tickers removed from the seed list are NOT deleted — watchlist FK rows
      would break and that's user data.
    * No table besides `tickers` is touched.
    """
    # create_all is idempotent — adds missing tables without dropping anything.
    Base.metadata.create_all(bind=get_engine())

    catalog: list[tuple[str, str, TickerType]] = (
        [(s, n, TickerType.ETF) for (s, n) in ETFS]
        + [(s, n, TickerType.STOCK) for (s, n) in STOCKS]
    )

    db = get_session_factory()()
    inserted = 0
    updated = 0
    try:
        existing: dict[str, Ticker] = {
            row.symbol: row
            for row in db.execute(select(Ticker)).scalars().all()
        }
        for sym, name, ttype in catalog:
            row = existing.get(sym)
            if row is None:
                db.add(Ticker(symbol=sym, name=name, type=ttype, is_seeded=True))
                inserted += 1
                continue
            # Only update rows WE own. User-added tickers keep whatever
            # name/type the user gave them.
            if not row.is_seeded:
                continue
            if row.name != name or row.type != ttype:
                row.name = name
                row.type = ttype
                updated += 1
        db.commit()
        log.info(
            "Seed: %d inserted, %d updated, %d already current (catalog size=%d)",
            inserted, updated, len(catalog) - inserted - updated, len(catalog),
        )
        _seed_demo_watchlist(db)
        db.commit()
    finally:
        db.close()
    return inserted


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    n = seed()
    print(f"Seeded {n} new tickers.")
