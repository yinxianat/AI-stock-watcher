import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import TickerAutocomplete from '../components/TickerAutocomplete.jsx';
import { logRenderError } from '../lib/log.js';
import { ErrorBoundary } from 'react-error-boundary';
import ErrorFallback from '../components/ErrorFallback.jsx';

export default function Dashboard() {
  const [watch, setWatch] = useState([]);
  const [trends, setTrends] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

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
            <table>
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
                {watch.map((w) => {
                  const tr = trendByTicker[w.ticker.id];
                  return (
                    <tr key={w.id}>
                      <td><strong>{w.ticker.symbol}</strong></td>
                      <td>{w.ticker.name}</td>
                      <td>{tr ? `$${tr.price.toFixed(2)}` : '—'}</td>
                      <td style={{ color: tr && tr.pct_change >= 0 ? 'var(--ok)' : 'var(--danger)' }}>
                        {tr ? `${tr.pct_change >= 0 ? '+' : ''}${tr.pct_change.toFixed(2)}%` : '—'}
                      </td>
                      <td>
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
                      <td style={{ textAlign: 'right' }}>
                        <Link to={`/rules/${w.ticker.id}`}>Rules</Link>{' '}
                        <button className="secondary" onClick={() => remove(w.id)}>Remove</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
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
