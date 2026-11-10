import { describe, expect, it } from 'vitest';
import { parseHoldingDraft } from './holdings.js';

describe('parseHoldingDraft', () => {
  it('accepts a price on its own and sends no share count', () => {
    expect(parseHoldingDraft({ price: '98.40', shares: '' })).toEqual({
      ok: true,
      value: { purchase_price: 98.4, shares: null },
    });
  });

  it('accepts a price with shares', () => {
    expect(parseHoldingDraft({ price: '98.40', shares: '12' })).toEqual({
      ok: true,
      value: { purchase_price: 98.4, shares: 12 },
    });
  });

  it('accepts fractional shares, which brokers now sell', () => {
    expect(parseHoldingDraft({ price: '98', shares: '0.5' }).value.shares).toBe(0.5);
  });

  it('ignores surrounding whitespace rather than reading it as a number', () => {
    expect(parseHoldingDraft({ price: '  98.40  ', shares: '  12 ' }).value).toEqual({
      purchase_price: 98.4,
      shares: 12,
    });
  });

  // Number('') === 0, so a blank price must not slip through as a free zero.
  it.each(['', '   ', '0', '-5', 'lots', '12abc', 'NaN', 'Infinity'])(
    'rejects %j as a price',
    (price) => {
      const out = parseHoldingDraft({ price, shares: '' });
      expect(out.ok).toBe(false);
      expect(out.error).toMatch(/price you paid/);
    },
  );

  // Blank means "didn't say"; zero would claim a real position worth $0.00.
  it.each(['0', '-1', 'ten', '1e12'])('rejects %j as a share count', (shares) => {
    expect(parseHoldingDraft({ price: '98', shares }).ok).toBe(false);
  });

  it('calls out a decimal-point typo rather than passing it on', () => {
    const out = parseHoldingDraft({ price: '98400000', shares: '' });
    expect(out.ok).toBe(false);
    expect(out.error).toMatch(/decimal point/);
  });

  it('treats missing fields the same as blank ones', () => {
    expect(parseHoldingDraft({}).ok).toBe(false);
    expect(parseHoldingDraft({ price: '98' }).value.shares).toBe(null);
  });
});
