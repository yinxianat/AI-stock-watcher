import { useEffect, useRef, useState } from 'react';
import { applySuggestion, suggest } from '../lib/sqlSuggest.js';

// The query box: a textarea with schema-driven completion over it.
//
// Deliberately not CodeMirror or Monaco. Those bring syntax highlighting and
// several megabytes of dependency, and this project has four runtime
// packages and hand-written CSS. What the admin actually needs here is to be
// told the column names — they don't know the schema, which is the whole
// reason this console exists — and a textarea plus the ORM-derived schema
// does that. Highlighting is the part you can live without; knowing that the
// column is `started_at` and not `start_time` is not.
//
// A plain textarea also keeps the things a hand-built editor usually breaks:
// native undo, spellcheck off, text selection, mobile keyboards, and screen
// readers reading it as the labelled form field it is.

// Rows the box grows to. Past this it scrolls rather than pushing the Run
// button off the screen, which is where a long query otherwise puts it.
const MIN_ROWS = 8;
const MAX_ROWS = 22;

export default function SqlEditor({ value, onChange, onRun, schema, busy }) {
  const ref = useRef(null);
  const [menu, setMenu] = useState(null); // { start, end, items }
  const [active, setActive] = useState(0);
  // Enter only accepts a completion once the admin has chosen one with the
  // arrow keys. Otherwise Enter is a newline, which is what it is for 95% of
  // the time — an editor that swallows your line break because a dropdown
  // happened to be open is the kind of thing that stops you using it.
  const [navigated, setNavigated] = useState(false);

  const rows = Math.min(MAX_ROWS, Math.max(MIN_ROWS, value.split('\n').length + 1));

  const close = () => { setMenu(null); setNavigated(false); setActive(0); };

  const recompute = (text, caret) => {
    if (!schema) return close();
    const next = suggest(text, caret, schema);
    if (!next.items.length) return close();
    setMenu(next);
    setActive(0);
    setNavigated(false);
  };

  const accept = (item) => {
    const out = applySuggestion(value, menu, item);
    onChange(out.text);
    close();
    // The caret has to be restored after React has written the new value,
    // or the browser drops it to the end of the text and the admin loses
    // their place in the middle of a line.
    requestAnimationFrame(() => {
      const el = ref.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(out.caret, out.caret);
    });
  };

  const handleChange = (e) => {
    onChange(e.target.value);
    recompute(e.target.value, e.target.selectionStart);
  };

  const handleKeyDown = (e) => {
    const open = !!menu?.items.length;

    // Run. Cmd/Ctrl+Enter rather than Enter, because a query is usually
    // several lines and you need to be able to type the next one.
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      close();
      if (!busy) onRun();
      return;
    }

    if (open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const step = e.key === 'ArrowDown' ? 1 : -1;
        setActive((i) => (i + step + menu.items.length) % menu.items.length);
        setNavigated(true);
        return;
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && navigated)) {
        e.preventDefault();
        accept(menu.items[active]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        close();
        return;
      }
    }

    // Tab with no dropdown open indents instead of leaving the field. Two
    // spaces, matching the saved queries.
    if (e.key === 'Tab' && !open) {
      e.preventDefault();
      const el = e.target;
      const { selectionStart: s, selectionEnd: t } = el;
      onChange(`${value.slice(0, s)}  ${value.slice(t)}`);
      requestAnimationFrame(() => el.setSelectionRange(s + 2, s + 2));
    }
  };

  // Close the dropdown if the caret is moved by click or by an arrow key
  // that we didn't handle: the suggestions were computed for where the
  // caret used to be, and stale suggestions are worse than none.
  const handleSelect = (e) => {
    if (!menu) return;
    const caret = e.target.selectionStart;
    if (caret < menu.start || caret > menu.end) close();
  };

  useEffect(() => { if (!schema) close(); }, [schema]);

  return (
    <div className="sql-editor">
      <textarea
        ref={ref}
        className="sql-input"
        value={value}
        rows={rows}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        aria-label="SQL query"
        placeholder={'SELECT * FROM job_runs\nORDER BY started_at DESC\nLIMIT 20'}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onSelect={handleSelect}
        onBlur={() => setTimeout(close, 120)}
      />
      {!!menu?.items.length && (
        <ul className="sql-complete" role="listbox">
          {menu.items.map((item, i) => (
            <li
              key={`${item.kind}:${item.label}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? 'active' : undefined}
              // mousedown, not click: click fires after blur has already
              // closed the menu.
              onMouseDown={(e) => { e.preventDefault(); accept(item); }}
              onMouseEnter={() => setActive(i)}
            >
              <span className={`sql-kind sql-kind-${item.kind}`}>{item.kind[0]}</span>
              <span className="sql-label">{item.label}</span>
              <span className="sql-detail">{item.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
