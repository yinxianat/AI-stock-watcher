// Layout guards for the watchlist table.
//
// Adding the purchase-price columns broke the dashboard on small screens:
// rows and the price editor spilled outside the card. All three causes were
// CSS, and all three are invisible to a unit test of behaviour — so these
// assertions pin the specific mistakes instead, read straight out of the
// stylesheet and the page.
//
// This is a regression guard, not a layout engine: it proves the rules that
// went wrong are still right, not that the page looks good. Eyeball the page
// at 375px and 800px as well.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(resolve(here, '../styles.css'), 'utf8');
const dashboard = readFileSync(resolve(here, '../pages/Dashboard.jsx'), 'utf8');

/** The body of every `@media (...)` block whose query matches `test`. */
function mediaBlocks(test) {
  const out = [];
  const re = /@media([^{]*)\{/g;
  let m;
  while ((m = re.exec(css))) {
    if (!test(m[1])) continue;
    // Walk braces from the block's opening one so nested rules come too.
    let depth = 1;
    let i = re.lastIndex;
    for (; i < css.length && depth > 0; i++) {
      if (css[i] === '{') depth++;
      else if (css[i] === '}') depth--;
    }
    out.push(css.slice(re.lastIndex, i - 1));
  }
  return out;
}

const phone = mediaBlocks((q) => /max-width:\s*600px/.test(q)).join('\n');
const desktop = mediaBlocks((q) => /min-width:\s*60[01]px/.test(q)).join('\n');
// Everything outside any @media block, i.e. rules that apply at every width.
const unconditional = css.replace(/@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, '');

describe('watchlist table on small screens', () => {
  it('has a phone layout and a desktop layout to compare', () => {
    expect(phone.length).toBeGreaterThan(0);
    expect(desktop.length).toBeGreaterThan(0);
  });

  // Cause 1. `white-space: nowrap` on a cell in the row grid's `auto` track
  // stops it shrinking, so a long "+$1,234,567.00 (+12.34%)" pushed the row
  // wider than the card. The neighbouring .cell-price / .cell-change are
  // deliberately not nowrap for the same reason.
  it('never makes the holdings cells nowrap at every width', () => {
    const rules = unconditional.match(/\.cell-(gain|paid)[^{]*\{[^}]*\}/g) ?? [];
    for (const rule of rules) {
      expect(rule).not.toMatch(/white-space:\s*nowrap/);
    }
  });

  it('allows the gain figure to wrap and shrink on a phone', () => {
    expect(phone).toMatch(/\.cell-gain[\s\S]{0,200}?min-width:\s*0/);
    expect(phone).toMatch(/\.cell-gain[\s\S]{0,200}?overflow-wrap:\s*anywhere/);
    expect(phone).not.toMatch(/\.cell-gain[\s\S]{0,200}?white-space:\s*nowrap/);
  });

  it('may make them nowrap on desktop, where the table can scroll instead', () => {
    expect(desktop).toMatch(/\.cell-(paid|gain)[\s\S]{0,120}?white-space:\s*nowrap/);
  });

  // Cause 2. A <td> left as a table-cell inside a block <tr> gets wrapped in
  // an anonymous table box sized to max-content, which carried the price
  // editor out of the card.
  it('makes the editor row and its cell both blocks on a phone', () => {
    expect(phone).toMatch(/tr\.holding-row\s*\{[^}]*display:\s*block/);
    expect(phone).toMatch(/tr\.holding-row\s*>\s*td\s*\{[^}]*display:\s*block/);
  });

  // Cause 3. A table cannot shrink below its min-content width, so nine
  // columns overflowed the card between the phone breakpoint and a wide
  // laptop. The table scrolls; the page does not.
  it('wraps the table in a scroll container that only scrolls on desktop', () => {
    expect(dashboard).toMatch(/className="table-scroll"/);
    expect(desktop).toMatch(/\.table-scroll\s*\{[^}]*overflow-x:\s*auto/);
    expect(unconditional).not.toMatch(/\.table-scroll\s*\{[^}]*overflow-x:\s*auto/);
  });

  it('keeps the table inside its card at any width', () => {
    expect(unconditional).toMatch(/\.table-scroll\s*\{[^}]*max-width:\s*100%/);
  });

  // The extra width is for the two extra columns, so it applies only when
  // they are there — the default layout must not change for everyone else.
  it('widens the container only when the holdings columns are shown', () => {
    expect(unconditional).toMatch(/\.container\.wide\s*\{[^}]*max-width:\s*1120px/);
    expect(unconditional).toMatch(/\.container\.wide\.with-holdings\s*\{[^}]*max-width:\s*1280px/);
    expect(dashboard).toMatch(/container wide\$\{holdingsOn \? ' with-holdings' : ''\}/);
  });

  // The phone layout is a stack of cards built from named grid areas. A cell
  // with no area lands wherever the auto-placement algorithm puts it, which
  // is how a row ends up overlapping or escaping its card.
  it('gives every phone grid area a cell, and every holdings cell an area', () => {
    const areas = new Set(
      (phone.match(/grid-template-areas:[^;]+;/g) ?? [])
        .flatMap((block) => block.match(/"[^"]+"/g) ?? [])
        .flatMap((row) => row.replace(/"/g, '').trim().split(/\s+/)),
    );
    for (const area of areas) {
      expect(phone, `grid area "${area}" has no cell assigned to it`)
        .toMatch(new RegExp(`grid-area:\\s*${area}\\b`));
    }
    for (const cell of ['paid', 'gain']) {
      expect(areas.has(cell), `.cell-${cell} is not placed in any phone row`).toBe(true);
    }
  });
});
