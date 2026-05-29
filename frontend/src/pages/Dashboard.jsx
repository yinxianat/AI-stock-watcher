import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import TickerAutocomplete from '../components/TickerAutocomplete.jsx';

export default function Dashboard() {
  const [watch, setWatch] = useState([]);
  const [trends, setTrends] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    try {
      const [w, t] = await Promise.all([api.listWatchlist(), api.myTrends()]);
      setWatch(w);
      setTrends(t);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function addByPick(t) {
    await api.addWatch({ ticker_id: t.id });
    load();
  }
  async function addBySymbol(sym) {
    if (!sym) return;
    await api.addWatch({ symbol: sym.toUpperCase() });
    load();
  }
  async function remove(id) {
    await api.removeWatch(id);
    load();
  }

  const trendByTicker = Object.fromEntries(trends.map((t) => [t.ticker_id, t]));

  return (
    <div className="container">
      <div className="card">
        <h2>Your watchlist</h2>
        <p className="muted">
          Pick from the popular list or type any ticker to add it. Trend data
          is refreshed three times each US trading day.
        </p>
        <TickerAutocomplete onPick={addByPick} />
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
