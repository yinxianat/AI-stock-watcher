import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import TickerAutocomplete from '../components/TickerAutocomplete.jsx';
import { logRenderError } from '../lib/log.js';
import { ErrorBoundary } from 'react-error-boundary';
import ErrorFallback from '../components/ErrorFallback.jsx';

const SORT_KEY = 'sw_watchlist_sort';
const SORT_OPTIONS = [
  { value: 'added', label: 'Date added' },
  { value: 'movers', label: 'Biggest movers' },
  { value: 'gainers', label: 'Top gainers' },
  { value: 'losers', label: 'Top losers' },
  { value: 'symbol', label: 'Symbol (A–Z)' },
];

// Stocks without trend data yet always go to the bottom for price-based sorts.
function sortWatchlist(items, trendByTicker, sortBy) {
  if (sortBy === 'added') return items;
  if (sortBy === 'symbol') {
    return [...items].sort((a, b) => a.ticker.symbol.localeCompare(b.ticker.symbol));
  }
  const score = {
    movers: (pct) => -Math.abs(pct),
    gainers: (pct) => -pct,
    losers: (pct) => pct,
  }[sortBy];
  const pctOf = (w) => trendByTicker[w.ticker.id]?.pct_change;
  return [...items].sort((a, b) => {
    const pa = pctOf(a);
    const pb = pctOf(b);
    if (pa == null && pb == null) return 0;
    if (pa == null) return 1;
    if (pb == null) return -1;
    return score(pa) - score(pb);
  });
}

function readSavedSort() {
  try {
    const saved = localStorage.getItem(SORT_KEY);
    return SORT_OPTIONS.some((o) => o.value === saved) ? saved : 'added';
  } catch {
    return 'added';
  }
}

export default function Dashboard() {
  const [watch, setWatch] = useState([]);
  const [trends, setTrends] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [sortBy, setSortBy] = useState(readSavedSort);

  function changeSort(value) {
    setSortBy(value);
    try {
      localStorage.setItem(SORT_KEY, value);
    } catch {
      // Private browsing can block storage; sorting still works for this visit.
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    async function load(){
      try{
        const [w,t] = await Promise.all([api.listWatchlist({signal: controller.signal}), api.myTrends({signal:controller.signal})]);
        if(controller.signal.aborted) return;
        setWatch(w);
        setTrends(t);
        setError(null);
        setLoading(false);
      } catch (err){
        if(err.name === "AbortError") return;
        setError(err.message);
        setLoading(false);
      }
    }
    load();
    return () => controller.abort()
  }, [reloadKey]);
  
  const refresh = () => setReloadKey((prev) => prev+1);

  async function addByPick(t) {
    try{
       await api.addWatch({ ticker_id: t.id });
       refresh();
    }catch(err){
      setError(err.message);
    }
  }
  async function addBySymbol(sym) {
    if (!sym) return;
    try{
        await api.addWatch({ symbol: sym.toUpperCase() });
        refresh();
    }catch(err){
      setError(err.message);
    }
  }
  async function remove(id) {
    try{
        await api.removeWatch(id);
        refresh()
    }catch(err){
        setError(err.message);
    }
  }

  const trendByTicker = Array.isArray(trends)?Object.fromEntries(trends.map((t) => [t.ticker_id, t])) :{};
  const sortedWatch = sortWatchlist(watch, trendByTicker, sortBy);

  return (
    <div className="container">
      <div className="card">
        <h2>Your watchlist</h2>
        <p className="muted">
          Pick from the popular list or type any ticker to add it. Trend data
          is refreshed three times each US trading day.
        </p>
        <ErrorBoundary
          fallbackRender={(props) => (
            <ErrorFallback
              {...props}
              message="Search is unavailable right now" />
          )}
          onError={logRenderError}
          >
             <TickerAutocomplete onPick={addByPick} />
          </ErrorBoundary>
        <p className="muted" style={{ marginTop: 12 }}>
          Need a ticker not in the search? Type it as a plain symbol:{' '}
          <FastAddSymbol onAdd={addBySymbol} />
        </p>
      </div>

      {error && <div className="flash err">{error}</div>}
      {loading ? (
        <p>Loading…</p>
      ) : (
        <div className="card">
          {watch.length === 0 ? (
            <p className="muted">Nothing on your watchlist yet.</p>
          ) : (
            <>
            <div className="list-toolbar">
              <label htmlFor="watch-sort">Sort by</label>
              <select id="watch-sort" value={sortBy} onChange={(e) => changeSort(e.target.value)}>
                {SORT_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
            <table className="watch-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Name</th>
                  <th>Last price</th>
                  <th>% change</th>
                  <th>Flags</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {sortedWatch.map((w) => {
                  const tr = trendByTicker[w.ticker.id];
                  return (
                    <tr key={w.id}>
                      <td className="cell-symbol"><strong>{w.ticker.symbol}</strong></td>
                      <td className="cell-name" title={w.ticker.name}>{w.ticker.name}</td>
                      <td className="cell-price">{tr ? `$${tr.price.toFixed(2)}` : '—'}</td>
                      <td className="cell-change" style={{ color: tr && tr.pct_change >= 0 ? 'var(--ok)' : 'var(--danger)' }}>
                        {tr ? `${tr.pct_change >= 0 ? '+' : ''}${tr.pct_change.toFixed(2)}%` : '—'}
                      </td>
                      <td className="cell-flags">
                        {tr
                          ? [
                              tr.is_week_high && 'W-Hi',
                              tr.is_week_low && 'W-Lo',
                              tr.is_month_high && 'M-Hi',
                              tr.is_month_low && 'M-Lo',
                              tr.is_quarter_high && 'Q-Hi',
                              tr.is_quarter_low && 'Q-Lo',
                              tr.is_year_high && 'Y-Hi',
                              tr.is_year_low && 'Y-Lo',
                            ]
                              .filter(Boolean)
                              .join(' · ') || '—'
                          : '—'}
                      </td>
                      <td className="actions cell-actions">
                        <Link to={`/rules/${w.ticker.id}`} aria-label={`Rules for ${w.ticker.symbol}`}>Rules</Link>{' '}
                        <button className="secondary" onClick={() => remove(w.id)} aria-label={`Remove ${w.ticker.symbol}`}>Remove</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function FastAddSymbol({ onAdd }) {
  const [v, setV] = useState('');
  return (
    <span style={{ display: 'inline-flex', gap: 8 }}>
      <input
        style={{ width: 120, display: 'inline-block' }}
        value={v}
        onChange={(e) => setV(e.target.value)}
        placeholder="TSLA"
      />
      <button
        className="secondary"
        onClick={() => {
          onAdd(v.trim());
          setV('');
        }}
      >
        Add
      </button>
    </span>
  );
}
