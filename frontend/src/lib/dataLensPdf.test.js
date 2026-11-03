import { describe, expect, it } from 'vitest';
import { pdfFilename, provenance, readingBlocks } from './dataLensPdf.js';

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
  notes: ['Email addresses are masked.'],
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
    expect(lines[0]).toBe('1 row(s) in 12 ms');
    expect(lines).toContain('Exported 2026-11-03T20:30:00.000Z');
    expect(lines.join(' ')).toContain('UTC');
    expect(lines.join(' ')).toContain('America/New_York');
  });

  it('says so on the page when rows were left behind', () => {
    const lines = provenance({ ...RESULT, truncated: true }, AT);
    expect(lines[0]).toContain('truncated, there is more');
  });

  it('does not claim masking when emails were shown in full', () => {
    const lines = provenance({ ...RESULT, masked: false, notes: [] }, AT);
    expect(lines.join(' ')).not.toContain('masked');
  });

  it('works on a bare result', () => {
    expect(provenance({ row_count: 0, duration_ms: 1 }, AT).length).toBeGreaterThan(0);
  });
});
