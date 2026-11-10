// Turning what someone typed into what the API accepts.
//
// Extracted from the watchlist page so it can be tested on its own: this is
// the one piece of judgement on the client side, and getting it wrong means
// either a confusing 422 or — worse — a number the user did not mean. The
// server validates all of this again; the point here is a good message
// instead of a round trip.

// A blank share count is "I'd rather not say", which gives the percentage
// and the per-share change but no total. It is not zero: zero shares would
// report $0.00 on a real position.
const BLANK_SHARES = null;

export function parseHoldingDraft({ price, shares }) {
  const trimmedPrice = String(price ?? '').trim();
  if (trimmedPrice === '') {
    return { ok: false, error: 'Enter the price you paid per share, e.g. 98.40.' };
  }
  // Number('') is 0 and Number('12abc') is NaN — both must be rejected, and
  // the empty case is already handled above so a 0 here is a real zero.
  const purchase_price = Number(trimmedPrice);
  if (!Number.isFinite(purchase_price) || purchase_price <= 0) {
    return { ok: false, error: 'Enter the price you paid per share, e.g. 98.40.' };
  }
  if (purchase_price > 10_000_000) {
    return { ok: false, error: "That price looks like a typo. Check the decimal point." };
  }

  const trimmedShares = String(shares ?? '').trim();
  if (trimmedShares === '') {
    return { ok: true, value: { purchase_price, shares: BLANK_SHARES } };
  }
  const count = Number(trimmedShares);
  if (!Number.isFinite(count) || count <= 0) {
    return { ok: false, error: 'Leave shares blank, or enter how many you own.' };
  }
  if (count > 1_000_000_000) {
    return { ok: false, error: 'That many shares looks like a typo.' };
  }
  return { ok: true, value: { purchase_price, shares: count } };
}
