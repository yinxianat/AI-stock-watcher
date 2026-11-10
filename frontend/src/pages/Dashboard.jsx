import { Fragment, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import { parseHoldingDraft } from '../lib/holdings.js';
import TickerAutocomplete from '../components/TickerAutocomplete.jsx';
import { logRenderError } from '../lib/log.js';
import { ErrorBoundary } from 'react-error-boundary';
import ErrorFallback from '../components/ErrorFallback.jsx';
import { describeRule } from '../lib/rules.js';

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

// After adding a stock, the backend fetches its price in the background, so the
// trend isn't ready when the add request returns. Re-check at these intervals
// (about 20s in total) until it shows up.
const TREND_POLL_DELAYS_MS = [1000, 2000, 3000, 4000, 5000, 5000];

function wait(ms, signal) {
  return new Promise((resolve, reject) => {
    const id = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(id);
      reject(signal.reason);
    }, { once: true });
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
  // null = couldn't load rules; the alert line is hidden rather than wrong.
  const [rules, setRules] = useState([]);
  const [alertsEnabled, setAlertsEnabled] = useState(true);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [sortBy, setSortBy] = useState(readSavedSort);
  // Ticker ids just added whose trend data the backend is still fetching.
  const [pendingTrendIds, setPendingTrendIds] = useState([]);
  // Purchase-price comparison. Off by default, so the extra columns only
  // appear once the user has asked for them in Settings.
  const [holdingsOn, setHoldingsOn] = useState(false);
  const [holdings, setHoldings] = useState([]);
  // Ticker id whose price is being edited, and the form's current values.
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState({ price: '', shares: '' });
  const [holdingError, setHoldingError] = useState(null);
  const [savingHolding, setSavingHolding] = useState(false);

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
        const [w,t,r,prefs,hp,h] = await Promise.all([
          api.listWatchlist({signal: controller.signal}),
          api.myTrends({signal:controller.signal}),
          // Rules are extra info here: if they fail, still show the watchlist.
          api.listRules({signal: controller.signal}).catch((err) => {
            if (err.name === "AbortError") throw err;
            return null;
          }),
          api.getEmailPreferences({signal: controller.signal}).catch((err) => {
            if (err.name === "AbortError") throw err;
            return null;
          }),
          // Both holdings calls are extra: a watchlist that loads without
          // the gain column is still a working watchlist.
          api.getHoldingsPreferences({signal: controller.signal}).catch((err) => {
            if (err.name === "AbortError") throw err;
            return null;
          }),
          api.listHoldings({signal: controller.signal}).catch((err) => {
            if (err.name === "AbortError") throw err;
            return null;
          }),
        ]);
        if(controller.signal.aborted) return;
        setWatch(w);
        setTrends(t);
        setRules(r);
        // If preferences fail to load, assume on rather than show a false warning.
        setAlertsEnabled(prefs?.alerts_enabled ?? true);
        // Unknown preference means hide the feature: showing an empty "what
        // you paid" column to someone who never turned it on is worse than
        // not showing it.
        setHoldingsOn(hp?.enabled ?? false);
        setHoldings(Array.isArray(h) ? h : []);
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

  // Poll /trends until every newly added ticker has data, or give up after
  // the last delay (e.g. the price source is down) and show "—" as before.
  useEffect(() => {
    if (pendingTrendIds.length === 0) return;
    const controller = new AbortController();
    async function poll() {
      try {
        for (const delay of TREND_POLL_DELAYS_MS) {
          await wait(delay, controller.signal);
          const t = await api.myTrends({ signal: controller.signal });
          setTrends(t);
          const ready = new Set(t.map((x) => x.ticker_id));
          if (pendingTrendIds.every((id) => ready.has(id))) break;
        }
        setPendingTrendIds([]);
      } catch (err) {
        if (err.name === 'AbortError') return;
        setPendingTrendIds([]);
      }
    }
    poll();
    return () => controller.abort();
  }, [pendingTrendIds]);

  function waitForTrend(tickerId) {
    setPendingTrendIds((ids) => (ids.includes(tickerId) ? ids : [...ids, tickerId]));
  }

  async function addByPick(t) {
    try{
       await api.addWatch({ ticker_id: t.id });
       refresh();
       waitForTrend(t.id);
    }catch(err){
      setError(err.message);
    }
  }
  async function addBySymbol(sym) {
    if (!sym) return;
    try{
        const item = await api.addWatch({ symbol: sym.toUpperCase() });
        refresh();
        waitForTrend(item.ticker.id);
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

  const holdingByTicker = Object.fromEntries((holdings ?? []).map((h) => [h.ticker_id, h]));

  function startEdit(tickerId) {
    const h = holdingByTicker[tickerId];
    setEditing(tickerId);
    setHoldingError(null);
    setDraft({
      price: h ? String(h.purchase_price) : '',
      shares: h?.shares != null ? String(h.shares) : '',
    });
  }

  async function saveHolding(tickerId) {
    // Checked here for a good message; the server checks it again for real.
    const parsed = parseHoldingDraft(draft);
    if (!parsed.ok) {
      setHoldingError(parsed.error);
      return;
    }
    const { purchase_price, shares } = parsed.value;
    setSavingHolding(true);
    setHoldingError(null);
    try {
      const saved = await api.setHolding(tickerId, purchase_price, shares);
      setHoldings((rows) => [
        ...rows.filter((r) => r.ticker_id !== tickerId),
        saved,
      ]);
      setEditing(null);
    } catch (err) {
      setHoldingError(err.message);
    } finally {
      setSavingHolding(false);
    }
  }

  async function clearHolding(tickerId) {
    setSavingHolding(true);
    setHoldingError(null);
    try {
      await api.deleteHolding(tickerId);
      setHoldings((rows) => rows.filter((r) => r.ticker_id !== tickerId));
      setEditing(null);
    } catch (err) {
      setHoldingError(err.message);
    } finally {
      setSavingHolding(false);
    }
  }

  const trendByTicker = Array.isArray(trends)?Object.fromEntries(trends.map((t) => [t.ticker_id, t])) :{};
  const sortedWatch = sortWatchlist(watch, trendByTicker, sortBy);
  const rulesByTicker = {};
  for (const r of rules ?? []) (rulesByTicker[r.ticker_id] ??= []).push(r);

  return (
    <div className="container wide">
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
      {!alertsEnabled && (
        <div className="flash warn" role="status">
          Alert emails are turned off, so the alerts below won't be emailed.{' '}
          <Link to="/settings">Turn them on in Settings</Link>
        </div>
      )}
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
            <table className={`watch-table${holdingsOn ? ' with-holdings' : ''}`}>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Name</th>
                  <th>Last price</th>
                  <th>% change</th>
                  {holdingsOn && <th>You paid</th>}
                  {holdingsOn && <th>Gain / loss</th>}
                  <th>Flags</th>
                  <th>Alerts</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {sortedWatch.map((w) => {
                  const tr = trendByTicker[w.ticker.id];
                  const h = holdingByTicker[w.ticker.id];
                  const updating = !tr && pendingTrendIds.includes(w.ticker.id);
                  return (
                    <Fragment key={w.id}>
                    <tr>
                      <td className="cell-symbol"><strong>{w.ticker.symbol}</strong></td>
                      <td className="cell-name" title={w.ticker.name}>{w.ticker.name}</td>
                      <td className="cell-price">
                        {tr ? `$${tr.price.toFixed(2)}` : updating ? <span className="muted">Updating…</span> : '—'}
                      </td>
                      <td className="cell-change" style={tr ? { color: tr.pct_change >= 0 ? 'var(--ok)' : 'var(--danger)' } : undefined}>
                        {tr ? `${tr.pct_change >= 0 ? '+' : ''}${tr.pct_change.toFixed(2)}%` : '—'}
                      </td>
                      {holdingsOn && (
                        <td className="cell-paid">
                          {h ? (
                            <>
                              ${h.purchase_price.toFixed(2)}
                              {h.shares != null && (
                                <>
                                  <br />
                                  <span className="muted">{h.shares} sh</span>
                                </>
                              )}
                            </>
                          ) : (
                            <span className="muted">—</span>
                          )}
                        </td>
                      )}
                      {holdingsOn && (
                        <td
                          className="cell-gain"
                          style={h?.gain_loss ? { color: h.gain_loss.is_gain ? 'var(--ok)' : 'var(--danger)' } : undefined}
                        >
                          {h?.gain_loss ? (
                            <strong>{h.gain_loss.summary}</strong>
                          ) : h ? (
                            // Priced, but no current price yet — a stock added
                            // today, or a batch that hasn't run. Deliberately
                            // not "$0.00", which would read as "you're even".
                            <span className="muted">waiting for a price</span>
                          ) : (
                            <span className="muted">—</span>
                          )}
                        </td>
                      )}
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
                      <td className="cell-alerts">
                        <AlertSummary rules={rules === null ? null : rulesByTicker[w.ticker.id] ?? []} />
                      </td>
                      <td className="actions cell-actions">
                        <Link to={`/rules/${w.ticker.id}`} aria-label={`Rules for ${w.ticker.symbol}`}>Rules</Link>{' '}
                        {holdingsOn && (
                          <>
                            <button
                              className="link-button inline"
                              onClick={() => startEdit(w.ticker.id)}
                              aria-label={`${h ? 'Edit' : 'Add'} what you paid for ${w.ticker.symbol}`}
                            >
                              {h ? 'Edit price' : 'Add price'}
                            </button>{' '}
                          </>
                        )}
                        <button className="secondary" onClick={() => remove(w.id)} aria-label={`Remove ${w.ticker.symbol}`}>Remove</button>
                      </td>
                    </tr>
                    {holdingsOn && editing === w.ticker.id && (
                      <tr className="holding-row">
                        <td colSpan={9}>
                          <div className="holding-editor">
                            <label htmlFor={`paid-${w.ticker.id}`}>Price paid per share</label>
                            <input
                              id={`paid-${w.ticker.id}`}
                              type="number"
                              step="0.01"
                              min="0.01"
                              inputMode="decimal"
                              value={draft.price}
                              onChange={(e) => setDraft({ ...draft, price: e.target.value })}
                              placeholder="98.40"
                              autoFocus
                            />
                            <label htmlFor={`shares-${w.ticker.id}`}>Shares (optional)</label>
                            <input
                              id={`shares-${w.ticker.id}`}
                              type="number"
                              step="any"
                              min="0"
                              inputMode="decimal"
                              value={draft.shares}
                              onChange={(e) => setDraft({ ...draft, shares: e.target.value })}
                              placeholder="leave blank for % only"
                            />
                            <button onClick={() => saveHolding(w.ticker.id)} disabled={savingHolding}>
                              {savingHolding ? 'Saving…' : 'Save'}
                            </button>
                            <button className="secondary" onClick={() => setEditing(null)} disabled={savingHolding}>
                              Cancel
                            </button>
                            {h && (
                              <button
                                className="secondary"
                                onClick={() => clearHolding(w.ticker.id)}
                                disabled={savingHolding}
                              >
                                Remove price
                              </button>
                            )}
                          </div>
                          <p className="muted" style={{ margin: '6px 0 0' }}>
                            Leave shares blank and you'll see the percentage and the
                            change per share. Only you ever see this.
                          </p>
                          {holdingError && <div className="flash err" role="alert">{holdingError}</div>}
                        </td>
                      </tr>
                    )}
                    </Fragment>
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

// One line per stock saying when an email will be sent, so users can tell a
// stock with no rules (never emails) from one with alerts set up.
function AlertSummary({ rules }) {
  if (rules === null) return null;
  const active = rules.filter((r) => r.enabled);
  const paused = rules.length - active.length;
  const pausedNote = paused > 0 ? ` (${paused} paused)` : '';

  if (active.length === 0) {
    return (
      <span className="alerts-none">
        {paused > 0
          ? `${paused === 1 ? 'Alert' : `All ${paused} alerts`} paused — you won't be emailed`
          : "No alerts set — you won't be emailed"}
      </span>
    );
  }
  return (
    <span className="alerts-list">
      <span className="alerts-icon" aria-hidden="true">🔔</span>{' '}
      <span className="visually-hidden">Emails you when: </span>
      {active.map(describeRule).join(' · ')}
      {pausedNote && <span className="muted">{pausedNote}</span>}
    </span>
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
