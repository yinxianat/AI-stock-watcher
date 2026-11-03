import { describe, expect, it } from 'vitest';
import {
  GRID_MAX_COLUMNS,
  gridCell,
  gridCellChars,
  layoutFor,
  pdfFilename,
  provenance,
  readingBlocks,
  recordFields,
  wrapText,
} from './dataLensPdf.js';

// Only the content decisions are tested here. `exportPdf` itself needs a
// browser to draw into, and what matters about it — that the question, the
// reading and every row reach the page — is decided by these helpers.

const RESULT = {
  columns: ['job_name', 'status'],
  rows: [['notify', 'FAILED']],
  row_count: 1,
  duration_ms: 12,
  truncated: false,
  masked: true,
  // Verbatim from app/services/data_lens.py, so the dedupe is tested
  // against the sentence the backend actually sends.
  notes: ['Email addresses are masked. Set DATA_LENS_SHOW_EMAILS=true to see them in full.'],
  tables: ['job_runs'],
  timezone: { stored: 'UTC', display: 'America/New_York' },
};

const AT = new Date('2026-11-03T20:30:00Z');

describe('pdfFilename', () => {
  it('leads with the date so a folder of exports sorts chronologically', () => {
    expect(pdfFilename(RESULT, AT)).toBe('data-lens-2026-11-03-2030-job_runs.pdf');
  });
  it('falls back to "query" when no table was identified', () => {
    expect(pdfFilename({ tables: [] }, AT)).toBe('data-lens-2026-11-03-2030-query.pdf');
    expect(pdfFilename({}, AT)).toContain('query.pdf');
  });
  it('names every table a join touched', () => {
    expect(pdfFilename({ tables: ['users', 'rules'] }, AT)).toContain('users-rules');
  });
});

describe('readingBlocks', () => {
  it('is empty when the result was never explained', () => {
    // So the caller does not have to branch before laying out the page.
    expect(readingBlocks(null)).toEqual([]);
    expect(readingBlocks(undefined)).toEqual([]);
  });

  it('keeps each finding next to its evidence', () => {
    // A printed claim about production with no way to check it has to be
    // chased before it can be dismissed, which costs more than it saves.
    const blocks = readingBlocks({
      headline: 'h', summary: 's', model: 'claude-haiku-5-5',
      rows_seen: 1, rows_total: 1,
      findings: [{ severity: 'high', detail: 'notify failed', evidence: 'row 1' }],
    });
    const i = blocks.findIndex((b) => b.text.includes('notify failed'));
    expect(blocks[i + 1].text).toBe('Evidence: row 1');
  });

  it('prints a suggested query as code under its question', () => {
    const blocks = readingBlocks({
      headline: 'h', summary: 's', model: 'm', rows_seen: 1, rows_total: 1,
      next_queries: [{ question: 'What did it log?', sql: 'SELECT  1\n  FROM users' }],
    });
    const i = blocks.findIndex((b) => b.text === 'What did it log?');
    expect(blocks[i + 1]).toEqual({ style: 'code', text: 'SELECT 1 FROM users' });
  });

  it('always says who wrote it and how much of the result they saw', () => {
    const blocks = readingBlocks({
      headline: 'h', summary: 's', model: 'claude-haiku-5-5',
      rows_seen: 100, rows_total: 480,
    });
    const last = blocks[blocks.length - 1];
    expect(last.text).toContain('claude-haiku-5-5');
    expect(last.text).toContain('100 of 480 rows');
  });

  it('survives a reading with no findings and no suggestions', () => {
    const blocks = readingBlocks({
      headline: 'All normal.', summary: 'Nothing to report.',
      model: 'm', rows_seen: 3, rows_total: 3,
    });
    expect(blocks.map((b) => b.text)).toContain('All normal.');
    expect(blocks.some((b) => b.text === 'Worth acting on')).toBe(false);
  });
});

describe('provenance', () => {
  it('states the count, the export time and the timezone convention', () => {
    // A page of numbers with no date and no timezone is unreadable a week
    // later, which is exactly when someone opens it.
    const lines = provenance(RESULT, AT);
    expect(lines[0]).toBe('1 row in 12 ms');
    expect(lines).toContain('Exported 2026-11-03T20:30:00.000Z');
    expect(lines.join(' ')).toContain('UTC');
    expect(lines.join(' ')).toContain('America/New_York');
  });

  it('counts rows in plain English', () => {
    expect(provenance({ ...RESULT, row_count: 4 }, AT)[0]).toBe('4 rows in 12 ms');
  });

  it('says so on the page when rows were left behind', () => {
    const lines = provenance({ ...RESULT, truncated: true }, AT);
    expect(lines[0]).toContain('truncated, there is more');
  });

  it('does not print the masking note twice', () => {
    // The backend sends its own, naming the setting to change; the first
    // render of this printed the sentence once from each source.
    const said = provenance(RESULT, AT).filter((l) => /mask/i.test(l));
    expect(said).toHaveLength(1);
    expect(said[0]).toContain('DATA_LENS_SHOW_EMAILS');
  });

  it('still says so when masked and the backend sent no note', () => {
    const lines = provenance({ ...RESULT, notes: [] }, AT);
    expect(lines.filter((l) => /mask/i.test(l))).toHaveLength(1);
  });

  it('does not claim masking when emails were shown in full', () => {
    const lines = provenance({ ...RESULT, masked: false, notes: [] }, AT);
    expect(lines.join(' ')).not.toContain('masked');
  });

  it('works on a bare result', () => {
    expect(provenance({ row_count: 0, duration_ms: 1 }, AT).length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// Choosing a layout
//
// The first version of this export printed everything as a grid, which made
// `select * from stock_articles` unreadable: 16 columns on A4 is ~17mm each,
// about eleven characters per line, so a timestamp wrapped to three lines and
// a 600-character JSON blob to fifty.
// ---------------------------------------------------------------------------

const wide = (n) => ({
  columns: Array.from({ length: n }, (_, i) => `c${i}`),
  rows: [Array.from({ length: n }, () => 'x')],
  row_count: 1,
  duration_ms: 1,
});

describe('layoutFor', () => {
  it('keeps a grid while every column can still get a readable width', () => {
    expect(layoutFor(wide(GRID_MAX_COLUMNS))).toBe('grid');
    expect(layoutFor(RESULT)).toBe('grid');
  });

  it('switches to records once there are too many columns', () => {
    // Lining values up under a heading is what makes a table worth reading;
    // past this width there is nothing left to line up.
    expect(layoutFor(wide(GRID_MAX_COLUMNS + 1))).toBe('records');
    expect(layoutFor(wide(16))).toBe('records');
  });

  it('does not fall over on a result with no columns yet', () => {
    expect(layoutFor({})).toBe('grid');
    expect(layoutFor(null)).toBe('grid');
  });
});

describe('gridCellChars', () => {
  it('gives a cell more room when fewer columns share the page', () => {
    // A fixed cap truncated a five-column result that had space to spare.
    expect(gridCellChars(4)).toBeGreaterThan(gridCellChars(8));
  });
  it('never shrinks a cell below something worth reading', () => {
    expect(gridCellChars(GRID_MAX_COLUMNS)).toBeGreaterThanOrEqual(40);
    expect(gridCellChars(100)).toBe(40);
    expect(gridCellChars(0)).toBeGreaterThan(0);
  });
});

describe('gridCell', () => {
  it('leaves a short value alone', () => {
    expect(gridCell('FAILED', 60)).toBe('FAILED');
    expect(gridCell(42, 60)).toBe('42');
  });

  it('shortens a long value and says how long it really was', () => {
    // "…" alone leaves the reader unable to tell a clipped sentence from a
    // clipped JSON document.
    const out = gridCell('x'.repeat(500), 60);
    expect(out).toContain('(500 chars)');
    expect(out.length).toBeLessThan(90);
  });

  it('prints null as null rather than blank', () => {
    // An empty cell and a null are different facts about the row.
    expect(gridCell(null, 60)).toBe('null');
    expect(gridCell(undefined, 60)).toBe('null');
    expect(gridCell('', 60)).toBe('');
  });
});

describe('wrapText', () => {
  it('wraps on words', () => {
    expect(wrapText('the quick brown fox jumps', 10))
      .toEqual(['the quick', 'brown fox', 'jumps']);
  });

  it('hard-breaks a run too long to fit', () => {
    // This is the case jsPDF does not reliably handle: an unbroken JSON blob
    // or URL overflows the page edge instead of wrapping, which is one of
    // the ways the first version came out unreadable.
    const lines = wrapText('a'.repeat(25), 10);
    expect(lines).toEqual(['aaaaaaaaaa', 'aaaaaaaaaa', 'aaaaa']);
  });

  it('keeps the remainder of a broken run as the start of the next line', () => {
    const lines = wrapText(`${'a'.repeat(12)} tail`, 10);
    expect(lines).toEqual(['aaaaaaaaaa', 'aa tail']);
  });

  it('wraps a realistic JSON value without overflowing', () => {
    const json = '{"diagnostics": {"cause_found": false, "facts_found": 0, '
      + '"urls_seen": 20, "dropped_by_rule": {}, "max_searches": 2}}';
    const lines = wrapText(json, 40);
    expect(lines.every((l) => l.length <= 40)).toBe(true);
    expect(lines.join(' ').replace(/\s+/g, '')).toBe(json.replace(/\s+/g, ''));
  });

  it('preserves existing line breaks', () => {
    expect(wrapText('one\ntwo', 20)).toEqual(['one', 'two']);
  });

  it('always returns at least one line', () => {
    // So the caller can place lines without a length check.
    expect(wrapText('', 10)).toEqual(['']);
    expect(wrapText(null, 10)).toEqual(['null']);
  });
});

describe('recordFields', () => {
  it('pairs every column with its value, nulls included', () => {
    const fields = recordFields(['id', 'error'], [7, null]);
    expect(fields).toEqual([
      { name: 'id', value: '7' },
      { name: 'error', value: 'null' },
    ]);
  });
});

describe('provenance, per layout', () => {
  it('explains why a wide result is not a table', () => {
    const lines = provenance({ ...wide(16), timezone: null }, AT).join(' ');
    expect(lines).toContain('16 columns is too many');
  });

  it('warns when the grid shortened something', () => {
    // Two columns share a generous budget, so this has to be genuinely long.
    const lines = provenance({
      ...RESULT, rows: [['notify', 'x'.repeat(800)]],
    }, AT).join(' ');
    expect(lines).toContain('Copy as TSV');
  });

  it('stays quiet when nothing was shortened', () => {
    expect(provenance(RESULT, AT).join(' ')).not.toContain('Copy as TSV');
  });
});
