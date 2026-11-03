import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, Navigate } from 'react-router-dom';
import { api, getToken } from '../lib/api.js';
import { logError } from '../lib/log.js';
import SqlEditor from '../components/SqlEditor.jsx';

// Data Lens — ask the production database a question without opening a
// psql prompt.
//
// Three things share the screen on purpose. The saved queries answer "what
// can I even ask?", the schema panel answers "what is this table?", and the
// editor is where you change one and re-run it. Separate pages for those
// would make the console a thing you read instead of a thing you use.

const PAGE_SIZE = 50;
const HISTORY_KEY = 'sw_data_lens_history';
const HISTORY_MAX = 25;

// localStorage throws in a private window and in some embedded browsers, so
// every touch is guarded: losing the history is a shrug, an exception on
// mount is a blank page.
function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveHistory(entries) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(entries.slice(0, HISTORY_MAX)));
  } catch {
    // Full or blocked. The console still works; it just won't remember.
  }
}

// The queries the admin ran, newest first, deduped by SQL so re-running the
// same thing five times while tweaking a WHERE clause doesn't bury the rest.
function remember(entries, sql, outcome) {
  const trimmed = sql.trim();
  const rest = entries.filter((e) => e.sql !== trimmed);
  return [{ sql: trimmed, at: Date.now(), ...outcome }, ...rest].slice(0, HISTORY_MAX);
}

function clock(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// One saved query. The question is what it answers; `teaches` is the SQL idea
// it demonstrates, which is the reason these are a library and not a menu.
function SavedQuery({ query, onPick }) {
  return (
    <li>
      <button type="button" className="link-button dl-saved" onClick={() => onPick(query)}>
        {query.title}
      </button>
      <p className="dl-saved-question">{query.question}</p>
      <p className="dl-saved-teaches">Teaches: {query.teaches}</p>
    </li>
  );
}

// One table, expandable into its columns. The prose comes from the backend
// catalogue; the columns come from the ORM, so neither can drift from what
// the database actually contains.
function SchemaTable({ table, onPreview, onInsert }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="dl-table">
      <div className="dl-table-head">
        <button type="button" className="link-button" onClick={() => setOpen(!open)}>
          {open ? '▾' : '▸'} {table.table}
        </button>
        <button
          type="button"
          className="link-button dl-preview"
          onClick={() => onPreview(table)}
          title="Write a query for the 50 newest rows and run it"
        >
          peek
        </button>
      </div>
      {open && (
        <div className="dl-table-body">
          {table.purpose && <p>{table.purpose}</p>}
          {table.lifecycle && <p className="muted">Lifecycle: {table.lifecycle}</p>}
          {table.written_by && <p className="muted">Written by: {table.written_by}</p>}
          <ul className="dl-columns">
            {table.columns.map((c) => (
              <li key={c.name}>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => onInsert(c.name)}
                  title="Insert this column name into the query"
                >
                  {c.name}
                </button>
                <span className="dl-coltype">
                  {c.type}
                  {c.primary_key ? ' · pk' : ''}
                  {c.references ? ` · → ${c.references}` : ''}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
}

// The grid. Deliberately not sortable by clicking a header: ORDER BY is one
// of the things this console exists to teach, and a client-side sort over
// one capped page would also be a lie about the rest of the table.
function Results({ result, page, setPage }) {
  const total = result.rows.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const start = page * PAGE_SIZE;
  const slice = result.rows.slice(start, start + PAGE_SIZE);

  return (
    <>
      <div className="dl-result-head">
        <strong>
          {total.toLocaleString()} row{total === 1 ? '' : 's'}
        </strong>
        <span className="muted">{result.duration_ms} ms</span>
        {result.tables?.length > 0 && (
          <span className="muted">from {result.tables.join(', ')}</span>
        )}
        {result.masked && (
          <span className="muted" title="Set DATA_LENS_SHOW_EMAILS=true to see real addresses">
            emails masked
          </span>
        )}
      </div>

      {result.truncated && (
        <p className="flash warn dl-note">
          Stopped at {total.toLocaleString()} rows — there is more. Add a
          narrower <code>where</code>, or a <code>limit</code>, or group the
          rows instead of listing them.
        </p>
      )}
      {(result.notes ?? []).map((n, i) => (
        <p key={i} className="muted dl-note">{n}</p>
      ))}

      {total === 0 ? (
        <p className="muted">
          No rows matched. That is an answer — but check the date range first:
          it is the usual reason a correct query looks empty.
        </p>
      ) : (
        <div className="dl-grid-wrap">
          <table className="dl-grid">
            <thead>
              <tr>
                <th className="dl-rownum" />
                {result.columns.map((c) => <th key={c}>{c}</th>)}
              </tr>
            </thead>
            <tbody>
              {slice.map((row, i) => (
                <tr key={start + i}>
                  <td className="dl-rownum">{start + i + 1}</td>
                  {row.map((cell, j) => (
                    <td key={j} className={cell === null ? 'dl-null' : undefined}>
                      {cell === null ? 'null' : String(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pages > 1 && (
        <div className="dl-pager">
          <button
            type="button" className="secondary"
            disabled={page === 0} onClick={() => setPage(page - 1)}
          >
            ← Previous
          </button>
          <span className="muted">
            Rows {start + 1}–{Math.min(start + PAGE_SIZE, total)} of {total.toLocaleString()}
          </span>
          <button
            type="button" className="secondary"
            disabled={page >= pages - 1} onClick={() => setPage(page + 1)}
          >
            Next →
          </button>
        </div>
      )}
    </>
  );
}

export default function DataLens() {
  const signedIn = !!getToken();
  const [schema, setSchema] = useState(null);
  const [library, setLibrary] = useState(null);
  const [denied, setDenied] = useState(null);

  const [sql, setSql] = useState('');
  const [limit, setLimit] = useState('');
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [rejected, setRejected] = useState(null);
  const [page, setPage] = useState(0);
  const [history, setHistory] = useState(loadHistory);
  const [openCategory, setOpenCategory] = useState('start');
  const [copied, setCopied] = useState(false);
  const resultRef = useRef(null);

  useEffect(() => {
    if (!signedIn) return;
    Promise.all([api.dataLensSchema(), api.dataLensQueries()])
      .then(([s, q]) => {
        setSchema(s);
        setLibrary(q);
        // Open with the first "start here" query already in the box. An
        // empty editor and no SQL skills is a dead end; a query that
        // already works is something to change one line of.
        const first = q.categories?.[0]?.queries?.[0];
        if (first) setSql(first.sql);
      })
      .catch((e) => {
        setDenied(e?.status === 403
          ? 'This account is not an admin.'
          : (e.message || 'Could not load the console.'));
        if (e?.status !== 403 && e?.status !== 401) logError('data-lens', e, 'load');
      });
  }, [signedIn]);

  const tableByName = useMemo(
    () => new Map((schema?.tables ?? []).map((t) => [t.table, t])),
    [schema],
  );

  const run = (text = sql) => {
    const query = text.trim();
    if (!query || running) return;
    setRunning(true);
    setRejected(null);
    setCopied(false);
    const parsed = Number(limit);
    const rows = Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : null;
    api.dataLensRun(query, rows)
      .then((r) => {
        setResult(r);
        setPage(0);
        setHistory((h) => {
          const next = remember(h, query, { ok: true, rows: r.row_count });
          saveHistory(next);
          return next;
        });
        // Scroll the result into view: on a laptop the grid starts below the
        // fold, and a query that "did nothing" has usually just landed
        // somewhere the admin can't see.
        requestAnimationFrame(() => resultRef.current?.scrollIntoView({
          behavior: 'smooth', block: 'nearest',
        }));
      })
      .catch((e) => {
        // A rejection carries both what was wrong and what to do instead.
        // Showing only the first is how an admin learns to stop asking.
        const detail = e?.detail;
        setRejected({
          error: detail?.error ?? e?.userMessage ?? e?.message ?? 'The query failed.',
          hint: detail?.hint ?? null,
        });
        setResult(null);
        setHistory((h) => {
          const next = remember(h, query, { ok: false });
          saveHistory(next);
          return next;
        });
        if (e?.status !== 422 && e?.status !== 401) logError('data-lens', e, 'run');
      })
      .finally(() => setRunning(false));
  };

  const pickSaved = (q) => {
    setSql(q.sql);
    setRejected(null);
    run(q.sql);
  };

  const peek = (table) => {
    // Order by the table's own time column when it has one, so "peek" means
    // the newest rows rather than whatever the database happens to return.
    const order = table.time_column ? `\nORDER BY ${table.time_column} DESC` : '';
    const query = `SELECT *\nFROM ${table.table}${order}\nLIMIT 50`;
    setSql(query);
    run(query);
  };

  const insertColumn = (name) => setSql((s) => (s ? `${s} ${name}` : name));

  // Tab-separated, so it pastes into a spreadsheet as columns — which is
  // what "compare the result" means in practice.
  const copyResult = () => {
    if (!result) return;
    const lines = [
      result.columns.join('\t'),
      ...result.rows.map((r) => r.map((c) => (c === null ? '' : String(c))).join('\t')),
    ];
    navigator.clipboard?.writeText(lines.join('\n'))
      .then(() => setCopied(true))
      .catch((e) => logError('data-lens', e, 'copy'));
  };

  if (!signedIn) return <Navigate to="/" replace />;

  if (denied) {
    return (
      <div className="container">
        <div className="card">
          <h2>Data Lens</h2>
          <p className="flash warn">{denied}</p>
          <p className="muted">
            Add this address to <code>ADMIN_EMAILS</code>, or grant it a row
            in <code>admin_roles</code>.
          </p>
        </div>
      </div>
    );
  }

  if (!schema || !library) {
    return <div className="container"><p>Loading the schema…</p></div>;
  }

  return (
    <div className="container lens">
      <div className="card">
        <h2 style={{ margin: 0 }}>Data Lens</h2>
        <p className="muted" style={{ margin: '4px 0 0' }}>
          Ask the database a question. {schema.note}
        </p>
        <p className="muted" style={{ margin: '4px 0 0', fontSize: 12 }}>
          {library.count} saved queries · {schema.tables.length} tables ·{' '}
          <Link to="/admin">back to monitoring</Link>
        </p>
      </div>

      <div className="dl-layout">
        <div className="dl-side">
          <div className="card">
            <h3 className="dl-side-title">Questions you can ask</h3>
            <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
              {library.note}
            </p>
            {library.categories.map((cat) => (
              <div key={cat.key} className="dl-category">
                <button
                  type="button"
                  className="link-button dl-category-head"
                  onClick={() => setOpenCategory(openCategory === cat.key ? null : cat.key)}
                >
                  {openCategory === cat.key ? '▾' : '▸'} {cat.label}
                  <span className="muted"> ({cat.queries.length})</span>
                </button>
                {openCategory === cat.key && (
                  <ul className="dl-saved-list">
                    {cat.queries.map((q) => (
                      <SavedQuery key={q.key} query={q} onPick={pickSaved} />
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>

          <div className="card">
            <h3 className="dl-side-title">Tables</h3>
            <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
              Column names come from the code, so this cannot go stale.
              {schema.blocked_columns?.length > 0
                && ` ${schema.blocked_columns.join(', ')} is never readable.`}
            </p>
            <ul className="dl-tables">
              {schema.tables.map((t) => (
                <SchemaTable
                  key={t.table} table={t} onPreview={peek} onInsert={insertColumn}
                />
              ))}
            </ul>
          </div>
        </div>

        <div className="dl-main">
          <div className="card">
            <SqlEditor
              value={sql}
              onChange={setSql}
              onRun={run}
              schema={schema}
              busy={running}
            />
            <div className="dl-toolbar">
              <button type="button" onClick={() => run()} disabled={running || !sql.trim()}>
                {running ? 'Running…' : 'Run'}
              </button>
              <span className="muted dl-shortcut">⌘/Ctrl + Enter</span>
              <label htmlFor="dl-limit" className="dl-limit-label">Max rows</label>
              <input
                id="dl-limit"
                className="dl-limit"
                type="number"
                min="1"
                max={schema.limits?.max_rows ?? 500}
                placeholder={String(schema.limits?.max_rows ?? 500)}
                value={limit}
                onChange={(e) => setLimit(e.target.value)}
              />
              {result && (
                <button type="button" className="secondary" onClick={copyResult}>
                  {copied ? 'Copied' : 'Copy as TSV'}
                </button>
              )}
            </div>
            <p className="muted dl-help">
              Tab completes a table or column name. Type <code>jr.</code> after
              naming a table to see just its columns.
            </p>
          </div>

          {rejected && (
            <div className="card dl-rejected">
              <strong>That query did not run.</strong>
              <p className="dl-rejected-error">{rejected.error}</p>
              {rejected.hint && <p className="muted">{rejected.hint}</p>}
            </div>
          )}

          {result && (
            <div className="card" ref={resultRef}>
              <Results result={result} page={page} setPage={setPage} />
              {result.timezone && (
                <p className="muted dl-tz">
                  Timestamps are {result.timezone.stored}, shown with their
                  offset. Dates are {result.timezone.display} calendar days.
                </p>
              )}
            </div>
          )}

          {history.length > 0 && (
            <div className="card">
              <h3 className="dl-side-title">What you have run</h3>
              <ul className="dl-history">
                {history.map((h) => (
                  <li key={h.at}>
                    <button
                      type="button"
                      className="link-button dl-history-sql"
                      onClick={() => { setSql(h.sql); setRejected(null); }}
                      title="Put this back in the editor"
                    >
                      {h.sql.replace(/\s+/g, ' ').slice(0, 120)}
                    </button>
                    <span className="muted">
                      {clock(h.at)} ·{' '}
                      {h.ok ? `${(h.rows ?? 0).toLocaleString()} rows` : 'rejected'}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="muted" style={{ fontSize: 12, margin: '8px 0 0' }}>
                Kept in this browser only — it never leaves your machine.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
