import { describe, expect, it } from 'vitest';
import { applySuggestion, suggest, tablesInScope, wordAt } from './sqlSuggest.js';

// A stand-in for the backend's /admin/data-lens/schema payload, small enough
// to reason about but shaped exactly like the real one.
const SCHEMA = {
  tables: [
    {
      table: 'job_runs',
      purpose: 'One row per scheduled job run.',
      columns: [
        { name: 'id', type: 'INTEGER', primary_key: true, references: null },
        { name: 'job_id', type: 'VARCHAR', references: null },
        { name: 'status', type: 'VARCHAR', references: null },
        { name: 'started_at', type: 'DATETIME', references: null },
      ],
    },
    {
      table: 'notification_logs',
      purpose: 'One row per alert email we sent.',
      columns: [
        { name: 'id', type: 'INTEGER', primary_key: true, references: null },
        { name: 'user_id', type: 'INTEGER', references: 'users.id' },
        { name: 'sent_at', type: 'DATETIME', references: null },
      ],
    },
    {
      table: 'users',
      purpose: 'One row per family.',
      columns: [
        { name: 'id', type: 'INTEGER', primary_key: true, references: null },
        { name: 'email', type: 'VARCHAR', references: null },
      ],
    },
  ],
};

const labels = (text, caret = text.length) =>
  suggest(text, caret, SCHEMA).items.map((i) => i.label);

describe('wordAt', () => {
  it('finds the partial word under the caret', () => {
    expect(wordAt('select stat', 11)).toEqual({ word: 'stat', start: 7, qualifier: null });
  });
  it('reads the caret, not the end of the text', () => {
    // Editing the middle of a line is the normal case, not the exception.
    expect(wordAt('select stat from job_runs', 11).word).toBe('stat');
  });
  it('picks up the table or alias before a dot', () => {
    expect(wordAt('select jr.sta', 13)).toEqual({ word: 'sta', start: 10, qualifier: 'jr' });
    expect(wordAt('select jr.', 10).qualifier).toBe('jr');
  });
  it('reports no word when the caret follows punctuation', () => {
    expect(wordAt('count(*)', 8).word).toBe('');
  });
});

describe('tablesInScope', () => {
  it('collects tables and their aliases', () => {
    const scope = tablesInScope(
      'select * from job_runs jr join notification_logs as nl on nl.id = jr.id',
      SCHEMA,
    );
    expect(scope.get('jr')).toBe('job_runs');
    expect(scope.get('nl')).toBe('notification_logs');
    expect(scope.get('job_runs')).toBe('job_runs');
  });
  it('does not mistake a following keyword for an alias', () => {
    // `from job_runs where …` must not register a table called "where".
    const scope = tablesInScope('select * from job_runs where status = ?', SCHEMA);
    expect([...scope.keys()]).toEqual(['job_runs']);
  });
  it('ignores tables that do not exist', () => {
    // A typo should go quiet rather than suggest the wrong table's columns.
    expect(tablesInScope('select * from job_runz', SCHEMA).size).toBe(0);
  });
});

describe('suggest', () => {
  it('offers only tables after FROM', () => {
    const items = suggest('select * from job', 17, SCHEMA).items;
    expect(items.every((i) => i.kind === 'table')).toBe(true);
    expect(items.map((i) => i.label)).toContain('job_runs');
  });

  it('offers only tables after JOIN', () => {
    const items = suggest('select * from job_runs join not', 31, SCHEMA).items;
    expect(items.map((i) => i.label)).toEqual(['notification_logs']);
  });

  it('describes a table by what it is for, not its column count', () => {
    // The admin's actual problem is not knowing what a table holds, and
    // this is the only moment that answer reaches them while they type.
    const [table] = suggest('select * from notif', 19, SCHEMA).items;
    expect(table.detail).toBe('One row per alert email we sent.');
  });

  it('offers a qualified name only that table\'s columns', () => {
    expect(labels('select jr.s from job_runs jr', 11)).toEqual(['status', 'started_at']);
  });

  it('offers nothing for an unknown qualifier', () => {
    expect(labels('select xx.s from job_runs jr', 11)).toEqual([]);
  });

  it('resolves a qualifier written as the full table name', () => {
    expect(labels('select job_runs.stat from job_runs', 20)).toEqual(['status']);
  });

  it('puts columns of the named tables ahead of keywords', () => {
    const items = suggest('select * from job_runs where stat', 33, SCHEMA).items;
    expect(items[0].label).toBe('status');
    expect(items[0].kind).toBe('column');
  });

  it('prefers a prefix match over a substring match', () => {
    // `sent_at` starts with the typed text; `started_at` only contains it.
    const items = suggest('select * from notification_logs where sen', 41, SCHEMA).items;
    expect(items[0].label).toBe('sent_at');
  });

  it('warns when a column name exists in two joined tables', () => {
    // `id` meaning either table is exactly the query that comes back
    // "column reference is ambiguous", so say so before it is run.
    const items = suggest(
      'select i from job_runs jr join users u on u.id = jr.id',
      8,
      SCHEMA,
    ).items;
    const id = items.find((i) => i.label === 'id');
    expect(id.detail).toContain('also in users');
  });

  it('stays quiet on an empty caret', () => {
    // A dropdown that opens on its own covers the query you are reading.
    expect(labels('select * from job_runs where ')).toEqual([]);
  });

  it('still offers keywords when no table has been named yet', () => {
    // Uppercase, matching the saved queries the admin is copying from.
    expect(labels('sel')).toContain('SELECT');
  });

  it('survives a schema that has not loaded yet', () => {
    expect(suggest('select stat', 11, null).items).toEqual([]);
    expect(suggest('select stat', 11, {}).items).toEqual([]);
  });
});

describe('applySuggestion', () => {
  it('replaces the partial word and reports where the caret lands', () => {
    const text = 'select stat from job_runs';
    const span = suggest(text, 11, SCHEMA);
    const out = applySuggestion(text, span, { label: 'status' });
    expect(out.text).toBe('select status from job_runs');
    // Mid-edit, the caret has to stay put — dropping it to the end of the
    // query loses your place.
    expect(out.caret).toBe(13);
  });
  it('keeps a qualifier intact', () => {
    const text = 'select jr.sta from job_runs jr';
    const span = suggest(text, 13, SCHEMA);
    const out = applySuggestion(text, span, { label: 'status' });
    expect(out.text).toBe('select jr.status from job_runs jr');
  });
});
