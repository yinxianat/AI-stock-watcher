import { Fragment, useEffect, useState } from 'react';
import { Link, Navigate } from 'react-router-dom';
import { api, getToken } from '../lib/api.js';
import { logError } from '../lib/log.js';

// Severity drives colour and ordering. The backend already ranks findings,
// so this only has to render them in the order they arrive.
const SEVERITY = {
  high: {
    label: 'Act now', lane: 'Act now',
    colour: 'var(--danger)',
    bg: 'rgba(255, 107, 107, 0.15)',
    border: 'rgba(255, 107, 107, 0.45)',
  },
  medium: {
    label: 'Review', lane: 'Worth a look',
    colour: 'var(--warn)',
    bg: 'rgba(245, 196, 81, 0.12)',
    border: 'rgba(245, 196, 81, 0.4)',
  },
  low: {
    label: 'Note', lane: 'Notes',
    colour: 'var(--muted)',
    bg: 'var(--bg)',
    border: 'var(--border)',
  },
};

const LANES = ['high', 'medium', 'low'];

function Badge({ severity }) {
  const s = SEVERITY[severity] ?? SEVERITY.low;
  return (
    <span style={{
      background: s.bg, color: s.colour, border: `1px solid ${s.border}`,
      borderRadius: 999, padding: '2px 10px', fontSize: 12, fontWeight: 600,
      whiteSpace: 'nowrap',
    }}>
      {s.label}
    </span>
  );
}

// The ranked candidate explanations for a finding or a delivery row. Order
// is the backend's: it ranks by the evidence it has, so the first one is
// the thing to check first, not the most common cause in general.
function Causes({ causes }) {
  if (!causes?.length) return null;
  return (
    <ol style={{ margin: '8px 0 0', paddingLeft: 20, fontSize: 13 }}>
      {causes.map((c, i) => (
        <li key={i} style={{ marginBottom: 6 }}>
          {c.cause}
          <br />
          <span style={{ color: 'var(--muted)' }}>
            <strong>How to tell:</strong> {c.check}
          </span>
        </li>
      ))}
    </ol>
  );
}

// Raw facts behind a finding. Rendered as a definition list rather than a
// JSON blob so it stays readable, with arrays joined and objects left to
// JSON — those are the nested run records, and they are for copying out.
function Evidence({ evidence }) {
  const entries = Object.entries(evidence ?? {});
  if (!entries.length) return null;
  const show = (v) => {
    if (v === null || v === undefined) return '—';
    if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === 'object' ? JSON.stringify(x) : String(x))).join(', ') : 'none';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
  };
  return (
    <dl style={{ margin: '8px 0 0', fontSize: 12.5, display: 'grid',
                 gridTemplateColumns: 'auto 1fr', gap: '2px 10px' }}>
      {entries.map(([k, v]) => (
        <Fragment key={k}>
          <dt style={{ color: 'var(--muted)', whiteSpace: 'nowrap' }}>{k.replace(/_/g, ' ')}</dt>
          <dd style={{ margin: 0, wordBreak: 'break-word' }}>{show(v)}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

// The actions an admin can take on one finding.
//
// Reachable in a single click from the collapsed row, because the whole
// point is to get through a list quickly — anything that costs an expand
// plus a click plus a confirm does not get used, and the list goes back to
// being re-read every morning. The note is optional and travels with
// whichever button is pressed, so writing one costs nothing extra and the
// next reader gets "Railway free tier sleeps overnight, known" instead of
// a bare status.
function TriageControls({ item, states, onChange }) {
  const [note, setNote] = useState(item.triage?.note ?? '');
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const current = item.triage_state;

  const apply = (state, snoozeDays) => {
    setBusy(state);
    setError(null);
    api.adminSetFindingState(item.fingerprint, {
      state,
      note: note.trim() || null,
      ...(snoozeDays ? { snooze_days: snoozeDays } : {}),
    })
      .then((triage) => onChange(item.fingerprint, triage))
      .catch((e) => {
        setError(e.message || 'Could not save that');
        logError('admin', e, 'triage');
      })
      .finally(() => setBusy(null));
  };

  const act = (label, state, snoozeDays) => (
    <button
      type="button"
      className="triage-action"
      aria-pressed={current === state}
      disabled={busy !== null}
      onClick={(e) => { e.stopPropagation(); apply(state, snoozeDays); }}
      title={states?.[state]?.what}
    >
      {busy === state ? '…' : label}
    </button>
  );

  return (
    <div className="triage-strip" onClick={(e) => e.stopPropagation()}>
      {act('Ack 7d', 'ack', 7)}
      {act('Ack 30d', 'ack', 30)}
      {act('Working', 'working')}
      {act('To-do', 'backlog')}
      {act('Fixed', 'resolved')}
      <input
        type="text"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="why (optional)"
        maxLength={1000}
        className="triage-note"
        aria-label="Note for this finding"
      />
      {error && <span style={{ color: 'var(--danger)', fontSize: 12 }}>{error}</span>}
    </div>
  );
}

// How long a finding has been around, and what was decided about it.
// Day 1 and day 14 need different reactions and used to render
// identically, which is how something broken for a fortnight keeps being
// skimmed past.
function TriageSummary({ triage }) {
  if (!triage) return null;
  const bits = [];
  if (triage.days_seen > 1) {
    bits.push(`seen on ${triage.days_seen} days`);
  }
  if (triage.age_days > 1) {
    bits.push(`first ${triage.first_seen}`);
  }
  if (triage.state === 'ack' && triage.snooze_until) {
    bits.push(`back on ${triage.snooze_until}`);
  }
  if (triage.state === 'resolved' && triage.resolved_on) {
    bits.push(`marked fixed ${triage.resolved_on}`);
  }
  if (!bits.length && !triage.note) return null;
  return (
    <p style={{ margin: '3px 0 0', fontSize: 12, color: 'var(--muted)' }}>
      {bits.join(' · ')}
      {triage.note && (
        <>
          {bits.length ? ' · ' : ''}
          <em>“{triage.note}”</em>
        </>
      )}
    </p>
  );
}

// Click to expand. The one-line detail was the whole dashboard before, and
// "ingest never ran" is true without being useful — the facts and the
// ranked causes are what turn it into something to go and do.
function Finding({ item, states, onTriage }) {
  const [open, setOpen] = useState(false);
  const expandable = !!(item.causes?.length || Object.keys(item.evidence ?? {}).length);
  const t = item.triage;
  return (
    <div style={{ padding: '10px 0', borderTop: '1px solid var(--border)' }}>
      <div
        style={{ display: 'flex', gap: 10, cursor: expandable ? 'pointer' : 'default' }}
        onClick={expandable ? () => setOpen(!open) : undefined}
        role={expandable ? 'button' : undefined}
        tabIndex={expandable ? 0 : undefined}
        onKeyDown={expandable ? (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(!open); }
        } : undefined}
      >
        <Badge severity={item.severity} />
        <div style={{ minWidth: 0, flex: 1 }}>
          <p style={{ margin: 0, fontSize: 14 }}>
            {/* The most important thing a monitoring page can tell you:
                this was fixed and it is back. Without it, a recurrence
                reads as a first occurrence and the fix looks like it
                held. */}
            {t?.regressed && (
              <span className="triage-flag triage-flag-back">came back</span>
            )}
            {t?.snooze_expired && (
              <span className="triage-flag">snooze ran out</span>
            )}
            {item.triage_state === 'working' && (
              <span className="triage-flag triage-flag-working">working on it</span>
            )}
            {item.detail}
          </p>
          {item.remediation && (
            <p style={{ margin: '4px 0 0', fontSize: 13, color: 'var(--muted)' }}>
              <strong>Fix:</strong> {item.remediation}
            </p>
          )}
          {expandable && (
            <span style={{ fontSize: 12, color: 'var(--accent)' }}>
              {open ? '▾ hide details' : '▸ what happened and why'}
            </span>
          )}
          <TriageSummary triage={t} />
          {onTriage && (
            <TriageControls item={item} states={states} onChange={onTriage} />
          )}
        </div>
      </div>
      {open && (
        <div style={{ margin: '8px 0 0 34px', paddingLeft: 12,
                      borderLeft: '2px solid var(--border)' }}>
          {Object.keys(item.evidence ?? {}).length > 0 && (
            <>
              <strong style={{ fontSize: 12.5 }}>What we know</strong>
              <Evidence evidence={item.evidence} />
            </>
          )}
          {item.causes?.length > 0 && (
            <>
              <strong style={{ fontSize: 12.5, display: 'block', marginTop: 10 }}>
                Possible causes, likeliest first
              </strong>
              <Causes causes={item.causes} />
            </>
          )}
          <code style={{ fontSize: 11, color: 'var(--muted)', display: 'block', marginTop: 10 }}>
            {item.code}
          </code>
        </div>
      )}
    </div>
  );
}

// One severity's worth of findings, headed by its own count.
//
// Before this, every finding on the day went into one list under a heading
// that counted only the urgent ones, so "1 thing needs attention now"
// could sit on top of eight visually identical rows. A card that promises
// triage and delivers a dump is worse than no card: the reader has to
// re-triage it by hand every morning, which is the job the card exists to
// do. Notes start collapsed because they are the lane you read last.
function Lane({ severity, items, startOpen, title, colour, states, onTriage }) {
  const [open, setOpen] = useState(startOpen);
  const s = SEVERITY[severity] ?? SEVERITY.low;
  if (!items.length) return null;
  return (
    <div style={{ marginTop: 14 }}>
      <button
        type="button"
        className="link-button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        style={{
          margin: 0, padding: 0, fontSize: 13, fontWeight: 700,
          color: colour ?? s.colour, display: 'flex', alignItems: 'center', gap: 6,
        }}
      >
        <span style={{ fontSize: 11 }}>{open ? '▾' : '▸'}</span>
        {title ?? s.lane} ({items.length})
      </button>
      {open && items.map((f) => (
        <Finding
          key={f.fingerprint ?? f.code}
          item={f}
          states={states}
          onTriage={onTriage}
        />
      ))}
    </div>
  );
}

// One family's email, expandable into why it carried a story or didn't.
function DeliveryRow({ row }) {
  const [open, setOpen] = useState(false);
  const ex = row.explain ?? {};
  return (
    <div style={{ padding: '8px 0', borderTop: '1px solid var(--border)' }}>
      <div
        style={{ display: 'flex', gap: 10, alignItems: 'baseline', cursor: 'pointer' }}
        onClick={() => setOpen(!open)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(!open); }
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, minWidth: 0, flex: 1 }}>
          {row.user}
        </span>
        <code style={{ fontSize: 12 }}>{row.symbol}</code>
        <span style={{ fontSize: 12, color: 'var(--muted)' }}>{row.channel}</span>
        {(row.sent_et || row.sent_at) && (
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            {row.sent_et ?? etClock(row.sent_at)}
          </span>
        )}
        <span style={{
          fontSize: 12, fontWeight: 600,
          color: ex.ok === false ? 'var(--warn)' : 'var(--muted)',
        }}>
          {ex.label ?? row.reason}
        </span>
        <span style={{ fontSize: 12, color: 'var(--accent)' }}>{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div style={{ margin: '6px 0 0', paddingLeft: 12,
                      borderLeft: '2px solid var(--border)', fontSize: 13 }}>
          {row.detail && <p style={{ margin: '0 0 6px' }}><strong>This send:</strong> {row.detail}</p>}
          {ex.what && <p style={{ margin: '0 0 6px', color: 'var(--muted)' }}>{ex.what}</p>}
          {row.inferred && (
            <p style={{ margin: '0 0 6px', color: 'var(--warn)' }}>
              No outcome was recorded for this email, so the reason is inferred from the
              story row rather than known.
            </p>
          )}
          {ex.causes?.length > 0 && (
            <>
              <strong style={{ fontSize: 12.5 }}>Possible causes, likeliest first</strong>
              <Causes causes={ex.causes} />
            </>
          )}
          {ex.fix && (
            <p style={{ margin: '8px 0 0', color: 'var(--muted)' }}>
              <strong>Fix:</strong> {ex.fix}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function Card({ title, subtitle, children }) {
  return (
    <div className="card">
      <h3 style={{ margin: '0 0 2px' }}>{title}</h3>
      {subtitle && <p className="muted" style={{ margin: '0 0 10px', fontSize: 13 }}>{subtitle}</p>}
      {children}
    </div>
  );
}

function Table({ head, rows, empty }) {
  if (!rows.length) return <p className="muted" style={{ fontSize: 13 }}>{empty}</p>;
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h} style={{
                textAlign: 'left', padding: '6px 10px 6px 0', borderBottom: '1px solid var(--border)',
                color: 'var(--muted)', fontWeight: 600, whiteSpace: 'nowrap',
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((cells, i) => (
            <tr key={i}>
              {cells.map((c, j) => (
                <td key={j} style={{
                  padding: '6px 10px 6px 0', borderBottom: '1px solid var(--border)',
                  whiteSpace: 'nowrap',
                }}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// A job firing a long way from its slot is the timezone class of bug, so
// the drift is called out rather than buried in a timestamp.
function drift(mins) {
  if (mins === null || mins === undefined) return '—';
  if (Math.abs(mins) < 5) return 'on time';
  return `${mins > 0 ? '+' : ''}${mins} min`;
}

// Always Eastern, always labelled. Two reasons not to use the viewer's own
// timezone: every job schedule in this app is written in ET, so a reader
// comparing "ran at" against "expected 09:35 ET" needs the same clock; and
// an unlabelled time on a monitoring page is the thing that sends someone
// looking at the wrong hour. The backend sends `*_et` strings for the same
// reason — this is the fallback for raw ISO values.
//
// It depends on those ISO values carrying an offset, which they now do.
// Without one, `new Date()` treats the string as LOCAL time and silently
// shifts every timestamp on this page by the viewer's offset.
function etClock(iso) {
  if (!iso) return '—';
  return `${new Date(iso).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', timeZone: 'America/New_York',
  })} ET`;
}

// Who got an AI story and who didn't. The counts come first because the
// first question is always "is today normal?"; the per-family rows below
// are for the days when it isn't.
function StoryDelivery({ today, yesterday }) {
  const [which, setWhich] = useState('today');
  const [all, setAll] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const [loadingAll, setLoadingAll] = useState(false);
  const day = which === 'today' ? today : yesterday;
  const sd = day.delivery?.stories;

  // Fetches on first open only; after that this is a pure show/hide, so
  // collapsing a long list and looking again costs nothing.
  const toggleAll = () => {
    if (all) { setShowAll(!showAll); return; }
    setLoadingAll(true);
    api.adminDelivery(day.date)
      .then((r) => { setAll(r); setShowAll(true); })
      .catch((e) => logError('admin', e, 'load-delivery'))
      .finally(() => setLoadingAll(false));
  };

  const tab = (key, label) => (
    <button
      type="button"
      onClick={() => { setWhich(key); setAll(null); setShowAll(false); }}
      style={{
        border: 'none', background: 'none', cursor: 'pointer', fontSize: 13,
        padding: '2px 0', marginRight: 14,
        fontWeight: which === key ? 700 : 400,
        borderBottom: which === key ? '2px solid var(--accent)' : '2px solid transparent',
        color: which === key ? 'var(--accent)' : 'var(--muted)',
      }}
    >
      {label}
    </button>
  );

  return (
    <Card
      title="AI stories: who got one"
      subtitle="Recorded by the job as it sent each email, not worked out afterwards."
    >
      <div style={{ marginBottom: 10 }}>
        {tab('today', `Today (${today.date})`)}
        {tab('yesterday', `Yesterday (${yesterday.date})`)}
      </div>

      {!sd || sd.emails === 0 ? (
        <p className="muted" style={{ fontSize: 13 }}>
          No alert or recap emails went out on this day.
        </p>
      ) : (
        <>
          <p style={{ margin: '0 0 10px', fontSize: 14 }}>{sd.summary}</p>
          <Table
            head={['Emails', 'With a story', 'Fixed note', 'Follow-up promised', 'Nothing for kids']}
            empty=""
            rows={[[
              sd.emails, sd.with_story, sd.with_fixed_note,
              sd.promised_followup, sd.nothing_for_kids,
            ]]}
          />
          {sd.received.length > 0 && (
            <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
              Got a story: {sd.received.join(', ')}
            </p>
          )}
          {sd.missed.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <strong style={{ fontSize: 13 }}>
                Got an email with nothing for kids — click a row for the reason
              </strong>
              {sd.missed.map((m, i) => <DeliveryRow key={i} row={m} />)}
            </div>
          )}
          <button
            type="button"
            className="disclosure"
            onClick={toggleAll}
            disabled={loadingAll}
            aria-expanded={showAll}
            style={{ marginTop: 12 }}
          >
            {loadingAll
              ? 'Loading…'
              : showAll
                ? '▾ Hide the full list'
                : '▸ Show every email, including the ones that worked'}
          </button>
          {all && showAll && (
            <div style={{ marginTop: 12 }}>
              <strong style={{ fontSize: 13 }}>Every email on this day</strong>
              {all.alerts.map((r, i) => (
                <DeliveryRow key={`a${i}`} row={{ ...r, channel: 'alert' }} />
              ))}
              {all.recaps.filter((r) => r.reason).map((r, i) => (
                <DeliveryRow key={`r${i}`} row={{ ...r, symbol: 'recap', channel: 'recap' }} />
              ))}
              {(all.stories?.followups ?? []).map((r, i) => (
                <DeliveryRow key={`f${i}`} row={r} />
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  );
}


// Which stories were written, and for the ones that weren't, why.
//
// This card used to be a two-column table reading `no_clear_cause | 4`.
// The count is the right thing to lead with, but it raises exactly one
// question and answered none of it, and "no clear cause" is not an
// explanation — it is the absence of one. Everything below was already
// being recorded by the pipeline and thrown away at the edge of the API.
function Stories({ today, yesterday, config }) {
  const [which, setWhich] = useState('yesterday');
  const [openStatus, setOpenStatus] = useState(null);
  const day = which === 'today' ? today : yesterday;
  const st = day.stories ?? {};
  const items = st.items ?? [];

  const tab = (key, label) => (
    <button
      type="button"
      className="link-button"
      onClick={() => { setWhich(key); setOpenStatus(null); }}
      style={{
        margin: '0 14px 0 0', padding: '2px 0', fontSize: 13,
        fontWeight: which === key ? 700 : 400,
        borderBottom: which === key ? '2px solid var(--accent)' : '2px solid transparent',
        color: which === key ? 'var(--accent)' : 'var(--muted)',
      }}
    >
      {label}
    </button>
  );

  const statuses = Object.entries(st.by_status ?? {});

  return (
    <Card
      title="Stories"
      subtitle={`Model ${config?.story_model ?? '—'} · click a status for the stories behind it`}
    >
      <div style={{ marginBottom: 10 }}>
        {tab('today', `Today (${today.date})`)}
        {tab('yesterday', `Yesterday (${yesterday.date})`)}
      </div>

      {!statuses.length ? (
        <p className="muted" style={{ fontSize: 13 }}>No stories were generated.</p>
      ) : (
        <>
          {statuses.map(([status, count]) => {
            const open = openStatus === status;
            const rows = items.filter((i) => i.status === status);
            return (
              <div key={status} style={{ borderTop: '1px solid var(--border)' }}>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => setOpenStatus(open ? null : status)}
                  aria-expanded={open}
                  style={{
                    margin: 0, padding: '8px 0', fontSize: 13.5, width: '100%',
                    textAlign: 'left', color: 'var(--text)',
                  }}
                >
                  <span style={{ fontSize: 11, color: 'var(--accent)', marginRight: 6 }}>
                    {open ? '▾' : '▸'}
                  </span>
                  <strong>{count}</strong>{' '}
                  {st.status_labels?.[status] ?? status}
                  <code style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 8 }}>
                    {status}
                  </code>
                </button>
                {open && (rows.length ? rows.map((i, n) => (
                  <div key={n} style={{ padding: '6px 0 10px 18px', fontSize: 13 }}>
                    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                      <code style={{ fontSize: 12.5 }}>{i.symbol}</code>
                      <span style={{
                        color: i.move_pct < 0 ? 'var(--danger)' : 'var(--ok)',
                      }}>
                        {i.move_pct > 0 ? '+' : ''}{i.move_pct}%
                      </span>
                      {i.diagnostics?.searches_used != null && (
                        <span className="muted">
                          {i.diagnostics.searches_used}/{i.diagnostics.max_searches} searches
                        </span>
                      )}
                      {i.diagnostics?.urls_seen != null && (
                        <span className="muted">{i.diagnostics.urls_seen} results read</span>
                      )}
                      {i.diagnostics?.facts_found != null && (
                        <span className="muted">
                          {i.diagnostics.facts_kept ?? 0}/{i.diagnostics.facts_found} facts kept
                        </span>
                      )}
                      {i.est_cost_usd != null && (
                        <span className="muted">${i.est_cost_usd.toFixed(4)}</span>
                      )}
                    </div>
                    <p style={{ margin: '3px 0 0', color: 'var(--muted)' }}>{i.why}</p>
                    {i.error && (
                      <p style={{ margin: '3px 0 0', color: 'var(--warn)', fontSize: 12.5 }}>
                        {i.error}
                      </p>
                    )}
                  </div>
                )) : (
                  <p className="muted" style={{ fontSize: 12.5, padding: '0 0 8px 18px' }}>
                    Counted from this day's rows, but the detail isn't in this payload.
                  </p>
                ))}
              </div>
            );
          })}

          {/* The two derived verdicts. Both answer a question the counts
              raise and neither was computable by eye from the old card. */}
          {st.search_budget?.verdict && (
            <p className="flash warn" style={{ marginTop: 12, fontSize: 13 }}>
              <strong>Search budget:</strong> {st.search_budget.verdict}
            </p>
          )}
          {Object.keys(st.dropped_by_rule ?? {}).length > 0 && (
            <div style={{ marginTop: 10 }}>
              <strong style={{ fontSize: 13 }}>
                Facts our own checks threw away
              </strong>
              <p className="muted" style={{ fontSize: 12.5, margin: '2px 0 6px' }}>
                Research found these and we dropped them. A rule dominating this
                list is a setting or a prompt to change, not a quiet market.
              </p>
              <Table
                head={['Rule', 'Facts dropped']}
                empty=""
                rows={Object.entries(st.dropped_by_rule).map(([r, n]) => [r, n])}
              />
            </div>
          )}

          <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>
            Tokens in/out: {(st.input_tokens ?? 0).toLocaleString()} /{' '}
            {(st.output_tokens ?? 0).toLocaleString()}
            {st.est_cost_usd != null && ` · estimated $${st.est_cost_usd.toFixed(4)}`}
          </p>
        </>
      )}
    </Card>
  );
}


export default function Admin() {
  const signedIn = !!getToken();
  const [watch, setWatch] = useState(null);
  const [me, setMe] = useState(null);
  const [error, setError] = useState(null);
  const [states, setStates] = useState(null);
  // Decisions made since the page loaded, keyed by fingerprint. Applied
  // over the fetched findings rather than refetching the whole day report,
  // which takes seconds and scans several tables.
  const [edits, setEdits] = useState({});

  useEffect(() => {
    if (!signedIn) return;
    api.me().then(setMe).catch((e) => {
      if (e?.status !== 401) logError('admin', e, 'load-me');
    });
    // Advisory: the triage strip still works without it, just without
    // the hover descriptions, so a failure here is not worth a message.
    api.adminTriageStates()
      .then((r) => setStates(Object.fromEntries(r.states.map((x) => [x.code, x]))))
      .catch(() => {});
    api.adminWatch()
      .then(setWatch)
      .catch((e) => {
        // 403 is the ordinary "you're not an admin" outcome, not a bug.
        setError(e?.status === 403
          ? 'This account is not an admin.'
          : (e.message || 'Could not load diagnostics'));
        if (e?.status !== 403 && e?.status !== 401) logError('admin', e, 'load-watch');
      });
  }, [signedIn]);

  if (!signedIn) return <Navigate to="/" replace />;
  if (error) {
    return (
      <div className="container">
        <div className="card">
          <h2>Monitoring</h2>
          <p className="flash warn">{error}</p>
          {me && !me.is_admin && (
            <p className="muted">
              Signed in as {me.email}. Add this address to <code>ADMIN_EMAILS</code>,
              or grant it a row in <code>admin_roles</code>.
            </p>
          )}
        </div>
      </div>
    );
  }
  if (!watch) return <div className="container"><p>Loading…</p></div>;

  const { today, yesterday, storage, config } = watch;

  // `act_now` is the high-severity subset of `findings`, so the page used
  // to read both and render them back to back — correct, but it meant the
  // severity split existed in the data and nowhere on the screen. One
  // list, bucketed once; anything with an unrecognised severity falls into
  // notes rather than vanishing.
  // A decision taken a moment ago wins over the payload it arrived in.
  const withEdits = (list) => (list ?? []).map((f) => {
    const t = edits[f.fingerprint];
    if (!t) return f;
    const spec = states?.[t.state];
    return {
      ...f,
      triage: t,
      triage_state: t.state,
      // `stays_visible` is the backend's word on which states keep a
      // finding on screen. Falling back to showing it is the safe
      // direction: never hide something because a lookup was missing.
      hidden: spec ? !spec.stays_visible : false,
    };
  });

  const todayFindings = withEdits(today.findings);

  // Being worked on comes out of the severity lanes into its own, because
  // it is the only state that keeps a finding on screen and mixing it back
  // in makes "act now" a list of things already in hand.
  const working = todayFindings.filter((f) => f.triage_state === 'working');
  const parked = todayFindings.filter((f) => f.hidden);
  const open_ = todayFindings.filter(
    (f) => !f.hidden && f.triage_state !== 'working',
  );

  const byLane = Object.fromEntries(LANES.map((sev) => [sev, []]));
  for (const f of open_) {
    (byLane[f.severity] ?? byLane.low).push(f);
  }

  const headline = byLane.high.length
    ? { colour: SEVERITY.high.colour,
        text: `⚠️ ${byLane.high.length} thing${byLane.high.length > 1 ? 's' : ''} need attention now` }
    : byLane.medium.length
      ? { colour: SEVERITY.medium.colour,
          text: `${byLane.medium.length} thing${byLane.medium.length > 1 ? 's' : ''} worth a look — nothing urgent` }
      : working.length
        ? { colour: SEVERITY.medium.colour,
            text: `Nothing new — ${working.length} being worked on` }
        : { colour: 'var(--ok)', text: '✅ Nothing needs attention right now' };

  const onTriage = (fingerprint, triage) =>
    setEdits((prev) => ({ ...prev, [fingerprint]: triage }));

  return (
    <div className="container">
      <div className="card">
        <h2>Monitoring</h2>
        <p className="muted" style={{ margin: 0 }}>
          Today is {today.date}; yesterday is {yesterday.date}. Checked{' '}
          {etClock(watch.checked_at_et)}.
        </p>
        {watch.timezone && (
          <p className="muted" style={{ margin: '4px 0 0', fontSize: 12 }}>
            Times shown in Eastern ({watch.timezone.display}); stored as{' '}
            {watch.timezone.stored}. Dates are Eastern calendar days.
          </p>
        )}
        <p className="muted" style={{ margin: '8px 0 0', fontSize: 13 }}>
          A question this page doesn't answer? Ask the database directly in{' '}
          <Link to="/admin/data-lens">Data Lens</Link>.
        </p>
      </div>

      {/* Today — urgent. Separated from yesterday on purpose: mixing them
          buries a live problem under history. Within the day, split by
          severity so the headline count and the rows below it agree. */}
      <div className="card" style={{
        borderLeft: `4px solid ${headline.colour}`,
      }}>
        <h3 style={{ margin: '0 0 4px' }}>{headline.text}</h3>
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>Today, {today.date}</p>
        {/* Your own queue first: what you said you were doing. */}
        <Lane
          severity="medium"
          title="Being worked on"
          colour="var(--accent)"
          items={working}
          startOpen
          states={states}
          onTriage={onTriage}
        />
        {LANES.map((sev) => (
          <Lane
            key={sev}
            severity={sev}
            items={byLane[sev]}
            // Notes are the lane you read last, so they arrive folded.
            startOpen={sev !== 'low'}
            states={states}
            onTriage={onTriage}
          />
        ))}
        {/* Folded, but never dropped. A parked finding you cannot find
            again is the same as one that was never reported. */}
        <Lane
          severity="low"
          title="Parked — acknowledged, on the to-do list or marked fixed"
          items={parked}
          startOpen={false}
          states={states}
          onTriage={onTriage}
        />
        {!todayFindings.length && (
          <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
            Jobs are on schedule and nothing has errored.
          </p>
        )}
        {todayFindings.length > 0 && !open_.length && !working.length && (
          <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
            Everything flagged today has been dealt with or parked.
          </p>
        )}
        {(today.not_due_yet ?? []).length > 0 && (
          <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            Not due yet: {today.not_due_yet.join(', ')}
          </p>
        )}
      </div>

      <Card title="Today's jobs" subtitle="Expected time is US/Eastern. Large drift means a timezone problem.">
        <Table
          head={['Job', 'Expected ET', 'Ran at', 'Status', 'Drift']}
          empty="No scheduled jobs for this day (weekend)."
          rows={(today.jobs ?? []).map((j) => [
            j.job_id,
            `${j.expected_et} ET`,
            j.actual_et ?? (j.actual_utc ? etClock(j.actual_utc) : '—'),
            j.status,
            drift(j.drift_minutes),
          ])}
        />
      </Card>

      <Card title="Yesterday" subtitle="For review, not urgent.">
        {(yesterday.findings ?? []).length === 0
          ? <p className="muted" style={{ fontSize: 13 }}>Nothing flagged.</p>
          : withEdits(yesterday.findings).map((f) => (
            <Finding
              key={f.fingerprint ?? f.code}
              item={f}
              states={states}
              onTriage={onTriage}
            />
          ))}
      </Card>

      <StoryDelivery today={today} yesterday={yesterday} />

      <Card
        title="Recap misses"
        subtitle="Eligible users who got no recap yesterday, and the gate that stopped it."
      >
        {(yesterday.delivery?.recap_missed ?? []).length === 0
          ? <p className="muted" style={{ fontSize: 13 }}>Every eligible user got their recap.</p>
          : yesterday.delivery.recap_missed.map((m, i) => (
            <DeliveryRow
              key={i}
              row={{
                user: m.user,
                symbol: `${m.watch_count ?? 0} watched`,
                channel: m.missed_days_running > 1 ? `${m.missed_days_running} days running` : 'recap',
                reason: m.reason,
                detail: null,
                explain: m.explain,
              }}
            />
          ))}
        {(yesterday.delivery?.recap_unexplained ?? 0) > 0 && (
          <p className="flash warn" style={{ marginTop: 10, fontSize: 13 }}>
            {yesterday.delivery.recap_unexplained} user(s) were eligible but got nothing,
            and no gate explains it.
          </p>
        )}
      </Card>

      <Stories today={today} yesterday={yesterday} config={config} />

      <Card
        title="Storage"
        subtitle="Tables with a retention window settle at a steady size; the others grow forever."
      >
        <Table
          head={['Table', 'Rows', '+24h', 'MB', 'Retention', 'Pruning']}
          empty="No data."
          rows={(storage?.tables ?? []).map((t) => [
            t.table,
            t.rows.toLocaleString(),
            t.rows_added_24h.toLocaleString(),
            t.mb,
            t.retention_days ? `${t.retention_days}d` : 'none',
            t.retention_enforced === false ? '⚠️ stalled' : t.unbounded ? '—' : 'ok',
          ])}
        />
        <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          Total {storage?.total_mb ?? '—'} MB
          {storage?.total_bytes_measured === false && ' (estimated)'}
        </p>
      </Card>
    </div>
  );
}
