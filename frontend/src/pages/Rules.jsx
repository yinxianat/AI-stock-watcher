import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../lib/api.js';

const EVENT_TYPES = [
  { value: 'price_change_range', label: 'Price change outside band (%)', needsBand: true },
  { value: 'week_low', label: 'Hits new weekly low' },
  { value: 'week_high', label: 'Hits new weekly high' },
  { value: 'month_low', label: 'Hits new monthly low' },
  { value: 'month_high', label: 'Hits new monthly high' },
  { value: 'quarter_low', label: 'Hits new quarterly low' },
  { value: 'quarter_high', label: 'Hits new quarterly high' },
  { value: 'year_low', label: 'Hits new yearly low' },
  { value: 'year_high', label: 'Hits new yearly high' },
];

export default function Rules() {
  const { tickerId } = useParams();
  const nav = useNavigate();
  const [rules, setRules] = useState([]);
  const [tickers, setTickers] = useState([]);
  const [draft, setDraft] = useState({
    event_type: 'price_change_range',
    pct_low: -5,
    pct_high: 5,
    enabled: true,
  });
  const [flash, setFlash] = useState(null);

  async function load() {
    const [rs, ws] = await Promise.all([api.listRules(), api.listWatchlist()]);
    setRules(rs.filter((r) => r.ticker_id === Number(tickerId)));
    setTickers(ws.map((w) => w.ticker));
  }
  useEffect(() => {
    load();
  }, [tickerId]);

  const ticker = tickers.find((t) => t.id === Number(tickerId));

  async function save() {
    try {
      const payload = {
        ticker_id: Number(tickerId),
        event_type: draft.event_type,
        pct_low: draft.event_type === 'price_change_range' ? Number(draft.pct_low) : null,
        pct_high: draft.event_type === 'price_change_range' ? Number(draft.pct_high) : null,
        enabled: draft.enabled,
      };
      await api.upsertRule(payload);
      setFlash({ kind: 'ok', msg: 'Saved.' });
      load();
    } catch (err) {
      setFlash({ kind: 'err', msg: err.message });
    }
  }

  async function remove(id) {
    await api.deleteRule(id);
    load();
  }

  const meta = EVENT_TYPES.find((e) => e.value === draft.event_type);

  return (
    <div className="container">
      <div className="card">
        <p>
          <a href="#" onClick={(e) => { e.preventDefault(); nav('/dashboard'); }}>← Back</a>
        </p>
        <h2>
          Notification rules{ticker ? ` — ${ticker.symbol}` : ''}
        </h2>
        {flash && <div className={`flash ${flash.kind}`}>{flash.msg}</div>}

        <label>Event type</label>
        <select
          value={draft.event_type}
          onChange={(e) => setDraft({ ...draft, event_type: e.target.value })}
        >
          {EVENT_TYPES.map((e) => (
            <option key={e.value} value={e.value}>{e.label}</option>
          ))}
        </select>

        {meta?.needsBand && (
          <div className="row" style={{ marginTop: 12 }}>
            <div>
              <label>Lower bound %</label>
              <input
                type="number"
                value={draft.pct_low}
                onChange={(e) => setDraft({ ...draft, pct_low: e.target.value })}
              />
            </div>
            <div>
              <label>Upper bound %</label>
              <input
                type="number"
                value={draft.pct_high}
                onChange={(e) => setDraft({ ...draft, pct_high: e.target.value })}
              />
            </div>
          </div>
        )}

        <div style={{ marginTop: 16 }}>
          <button onClick={save}>Save rule</button>
        </div>
      </div>

      <div className="card">
        <h3>Current rules</h3>
        {rules.length === 0 ? (
          <p className="muted">No rules yet for this ticker.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Event</th>
                <th>Range</th>
                <th>Enabled</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id}>
                  <td>{EVENT_TYPES.find((e) => e.value === r.event_type)?.label || r.event_type}</td>
                  <td>{r.pct_low != null ? `[${r.pct_low}%, ${r.pct_high}%]` : '—'}</td>
                  <td>{r.enabled ? 'yes' : 'no'}</td>
                  <td><button className="danger" onClick={() => remove(r.id)}>Delete</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
