import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';

// Debounced auto-complete input. Calls onPick(ticker) when user selects a row.
export default function TickerAutocomplete({ onPick, placeholder = 'Search by symbol or name…' }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState([]);
  const [open, setOpen] = useState(false);
  const timerRef = useRef(null);

  useEffect(() => {
    if (!q.trim()) {
      setResults([]);
      return;
    }
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      try {
        const r = await api.searchTickers(q.trim());
        setResults(r);
        setOpen(true);
      } catch (_) {
        setResults([]);
      }
    }, 180); // debounce so we don't hammer the API per keystroke
    return () => clearTimeout(timerRef.current);
  }, [q]);

  return (
    <div className="autocomplete">
      <input
        value={q}
        placeholder={placeholder}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => results.length && setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
      />
      {open && results.length > 0 && (
        <div className="autocomplete-list">
          {results.map((t) => (
            <div
              key={t.id}
              onMouseDown={() => {
                onPick(t);
                setQ('');
                setResults([]);
                setOpen(false);
              }}
            >
              <strong>{t.symbol}</strong> <span className="muted">— {t.name}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
