// Autocomplete for the Data Lens SQL box.
//
// Pure functions over (text, caret, schema), so they can be tested without a
// DOM — this project has no jsdom, and anyway the interesting half of an
// autocomplete is not the dropdown, it is deciding what to offer.
//
// `schema` is the backend's /admin/data-lens/schema payload, which is built
// from the ORM. Nothing here hard-codes a table or a column: a suggestion
// list that has drifted from the real schema is worse than no suggestions,
// because it teaches a name that does not exist and the admin trusts it.

// Offered when the caret isn't somewhere table- or column-shaped. Inserted
// uppercase to match the saved queries: the admin's first move is to copy a
// template and change one line, and a query half in each case reads like two
// people wrote it. Matching is case-insensitive either way.
const KEYWORDS = [
  'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'ORDER BY', 'GROUP BY',
  'HAVING', 'LIMIT', 'JOIN', 'LEFT JOIN', 'ON', 'AS', 'DISTINCT', 'DESC',
  'ASC', 'IS NULL', 'IS NOT NULL', 'IN', 'BETWEEN', 'LIKE', 'ILIKE',
  'COUNT(*)', 'SUM(', 'AVG(', 'MIN(', 'MAX(', 'COALESCE(', 'CASE', 'WHEN',
  'THEN', 'ELSE', 'END', 'UNION ALL', 'WITH', 'EXISTS', 'NOT EXISTS',
  'CURRENT_DATE', 'NOW()', 'INTERVAL',
];

// The word that follows a table name in FROM/JOIN is usually an alias — but
// not when it's one of these, and getting that wrong is how `where` ends up
// in the completion list as if it were a table.
const NOT_AN_ALIAS = new Set([
  'where', 'on', 'using', 'join', 'left', 'right', 'inner', 'outer', 'full',
  'cross', 'natural', 'lateral', 'group', 'order', 'limit', 'offset', 'fetch',
  'having', 'union', 'intersect', 'except', 'and', 'or', 'as', 'select',
  'with', 'window', 'for', 'returning',
]);

const WORD_BEFORE_CARET = /[A-Za-z_][A-Za-z0-9_]*$/;
const QUALIFIER = /([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*$/;
const FROM_JOIN = /\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?/gi;
// The keyword immediately before the caret's word, ignoring whitespace and
// commas, which is what decides whether we're naming a table or a column.
const PRECEDING = /\b([A-Za-z_]+)\s*,?\s*$/;

// What the caret is sitting in: the partial word, where it starts, and the
// `jr` of a half-typed `jr.status`.
export function wordAt(text, caret) {
  const before = text.slice(0, caret);
  const match = before.match(WORD_BEFORE_CARET);
  const word = match ? match[0] : '';
  const start = caret - word.length;
  const qualified = QUALIFIER.exec(before.slice(0, start));
  return { word, start, qualifier: qualified ? qualified[1].toLowerCase() : null };
}

// Every name the query can legally put before a dot, mapped to the real
// table it stands for: `job_runs` for itself, and `jr` as well if it was
// aliased. Unknown names are dropped rather than guessed at — a typo'd
// table should produce no suggestions, not suggestions for the wrong table.
export function tablesInScope(text, schema) {
  const known = new Map((schema?.tables ?? []).map((t) => [t.table, t]));
  const scope = new Map();
  FROM_JOIN.lastIndex = 0;
  let m;
  while ((m = FROM_JOIN.exec(text)) !== null) {
    const table = m[1].toLowerCase();
    if (!known.has(table)) continue;
    scope.set(table, table);
    const alias = m[2]?.toLowerCase();
    if (alias && !NOT_AN_ALIAS.has(alias)) scope.set(alias, table);
  }
  return scope;
}

function columnsOf(schema, table) {
  return (schema?.tables ?? []).find((t) => t.table === table)?.columns ?? [];
}

function columnItem(col, table, { qualify }) {
  const parts = [col.type];
  if (col.primary_key) parts.push('primary key');
  if (col.references) parts.push(`→ ${col.references}`);
  return {
    label: col.name,
    insert: qualify ? col.name : col.name,
    kind: 'column',
    detail: `${table} · ${parts.join(' · ')}`,
  };
}

function tableItem(t) {
  return {
    label: t.table,
    insert: t.table,
    kind: 'table',
    // The purpose line, not the column count: the admin's problem is not
    // knowing what `delivery_outcomes` is for, and this is the one place
    // that answer can reach them while they are typing the name.
    detail: t.purpose || `${t.columns?.length ?? 0} columns`,
  };
}

// Prefix matches before substring matches, each group alphabetical. Ranking
// matters more than it looks: the admin accepts the first item with Tab, so
// whatever lands at the top is effectively what the editor suggested.
function rank(items, word) {
  const w = word.toLowerCase();
  if (!w) return items.slice(0, 40);
  const starts = [];
  const contains = [];
  for (const it of items) {
    const label = it.label.toLowerCase();
    if (label.startsWith(w)) starts.push(it);
    else if (label.includes(w)) contains.push(it);
  }
  return [...starts, ...contains].slice(0, 40);
}

// What to offer, and which span of text accepting it should replace.
//
// Returns `items: []` rather than null when there is nothing to say, so the
// caller never has to null-check before reading `.items`.
export function suggest(text, caret, schema) {
  const { word, start, qualifier } = wordAt(text, caret);
  const scope = tablesInScope(text, schema);
  const empty = { start, end: caret, items: [] };

  // `jr.` — only that table's columns make sense, so offer nothing else even
  // when the prefix matches a dozen other names.
  if (qualifier) {
    const table = scope.get(qualifier) ?? (columnsOf(schema, qualifier).length ? qualifier : null);
    if (!table) return empty;
    const items = columnsOf(schema, table).map((c) => columnItem(c, table, { qualify: false }));
    return { start, end: caret, items: rank(items, word) };
  }

  // Nothing typed yet and no dot: stay quiet. A dropdown that opens on an
  // empty caret covers the query you are reading.
  if (!word) return empty;

  const keyword = PRECEDING.exec(text.slice(0, start))?.[1]?.toLowerCase();

  // After FROM or JOIN a table is the only thing that can follow.
  if (keyword === 'from' || keyword === 'join') {
    return { start, end: caret, items: rank((schema?.tables ?? []).map(tableItem), word) };
  }

  // Otherwise: columns of the tables this query already mentions, then the
  // tables themselves, then keywords. Columns come first because by the time
  // you are typing in a WHERE clause, a column is what you almost always
  // want, and the one you want is in a table you have already named.
  const seen = new Set();
  const columns = [];
  for (const table of new Set(scope.values())) {
    for (const col of columnsOf(schema, table)) {
      // The same column name in two joined tables is one suggestion, not
      // two — but say so, because `id` meaning either is exactly when a
      // query comes back "column reference is ambiguous".
      if (seen.has(col.name)) {
        const existing = columns.find((c) => c.label === col.name);
        if (existing) existing.detail = `${existing.detail} (also in ${table})`;
        continue;
      }
      seen.add(col.name);
      columns.push(columnItem(col, table, { qualify: false }));
    }
  }
  const keywords = KEYWORDS.map((k) => ({
    label: k, insert: k, kind: 'keyword', detail: 'SQL',
  }));
  return {
    start,
    end: caret,
    items: [
      ...rank(columns, word),
      ...rank((schema?.tables ?? []).map(tableItem), word),
      ...rank(keywords, word),
    ].slice(0, 40),
  };
}

// Apply a chosen item, returning the new text and where the caret goes. The
// caller needs both: setting the value without moving the caret drops it to
// the end of the query, which loses your place mid-edit.
export function applySuggestion(text, { start, end }, item) {
  const insert = item.insert ?? item.label;
  return {
    text: text.slice(0, start) + insert + text.slice(end),
    caret: start + insert.length,
  };
}
