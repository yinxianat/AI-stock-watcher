import { Fragment, useEffect, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { api, getToken } from '../lib/api.js';
import { logError } from '../lib/log.js';

// Severity drives colour and ordering. The backend already ranks findings,
// so this only has to render them in the order they arrive.
const SEVERITY = {
  high: { label: 'Act now', colour: '#cf222e', bg: '#ffebe9' },
  medium: { label: 'Review', colour: '#9a6700', bg: '#fff8c5' },
  low: { label: 'Note', colour: '#57606a', bg: '#f6f8fa' },
};

function Badge({ severity }) {
  const s = SEVERITY[severity] ?? SEVERITY.low;
  return (
    <span style={{
      background: s.bg, color: s.colour, border: `1px solid ${s.colour}33`,
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
          <span style={{ color: '#57606a' }}>
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
          <dt style={{ color: '#57606a', whiteSpace: 'nowrap' }}>{k.replace(/_/g, ' ')}</dt>
          <dd style={{ margin: 0, wordBreak: 'break-word' }}>{show(v)}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

// Click to expand. The one-line detail was the whole dashboard before, and
// "ingest never ran" is true without being useful — the facts and the
// ranked causes are what turn it into something to go and do.
function Finding({ item }) {
  const [open, setOpen] = useState(false);
  const expandable = !!(item.causes?.length || Object.keys(item.evidence ?? {}).length);
  return (
    <div style={{ padding: '10px 0', borderTop: '1px solid #eaeef2' }}>
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
          <p style={{ margin: 0, fontSize: 14 }}>{item.detail}</p>
          {item.remediation && (
            <p style={{ margin: '4px 0 0', fontSize: 13, color: '#57606a' }}>
              <strong>Fix:</strong> {item.remediation}
            </p>
          )}
          <code style={{ fontSize: 11, color: '#8c959f' }}>{item.code}</code>
          {expandable && (
            <span style={{ fontSize: 12, color: '#0969da', marginLeft: 8 }}>
              {open ? '▾ hide details' : '▸ what happened and why'}
            </span>
          )}
        </div>
      </div>
      {open && (
        <div style={{ margin: '8px 0 0 34px', paddingLeft: 12,
                      borderLeft: '2px solid #d0d7de' }}>
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
        </div>
      )}
    </div>
  );
}

// One family's email, expandable into why it carried a story or didn't.
function DeliveryRow({ row }) {
  const [open, setOpen] = useState(false);
  const ex = row.explain ?? {};
  return (
    <div style={{ padding: '8px 0', borderTop: '1px solid #eaeef2' }}>
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
        <span style={{ fontSize: 12, color: '#57606a' }}>{row.channel}</span>
        {(row.sent_et || row.sent_at) && (
          <span style={{ fontSize: 12, color: '#8c959f' }}>
            {row.sent_et ?? etClock(row.sent_at)}
          </span>
        )}
        <span style={{
          fontSize: 12, fontWeight: 600,
          color: ex.ok === false ? '#9a6700' : '#57606a',
        }}>
          {ex.label ?? row.reason}
        </span>
        <span style={{ fontSize: 12, color: '#0969da' }}>{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div style={{ margin: '6px 0 0', paddingLeft: 12,
                      borderLeft: '2px solid #d0d7de', fontSize: 13 }}>
          {row.detail && <p style={{ margin: '0 0 6px' }}><strong>This send:</strong> {row.detail}</p>}
          {ex.what && <p style={{ margin: '0 0 6px', color: '#57606a' }}>{ex.what}</p>}
          {row.inferred && (
            <p style={{ margin: '0 0 6px', color: '#9a6700' }}>
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
            <p style={{ margin: '8px 0 0', color: '#57606a' }}>
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
                textAlign: 'left', padding: '6px 10px 6px 0', borderBottom: '1px solid #d0d7de',
                color: '#57606a', fontWeight: 600, whiteSpace: 'nowrap',
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((cells, i) => (
            <tr key={i}>
              {cells.map((c, j) => (
                <td key={j} style={{
                  padding: '6px 10px 6px 0', borderBottom: '1px solid #eaeef2',
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
  const [loadingAll, setLoadingAll] = useState(false);
  const day = which === 'today' ? today : yesterday;
  const sd = day.delivery?.stories;

  const loadAll = () => {
    setLoadingAll(true);
    api.adminDelivery(day.date)
      .then((r) => setAll(r))
      .catch((e) => logError('admin', e, 'load-delivery'))
      .finally(() => setLoadingAll(false));
  };

  const tab = (key, label) => (
    <button
      type="button"
      onClick={() => { setWhich(key); setAll(null); }}
      style={{
        border: 'none', background: 'none', cursor: 'pointer', fontSize: 13,
        padding: '2px 0', marginRight: 14,
        fontWeight: which === key ? 700 : 400,
        borderBottom: which === key ? '2px solid #0969da' : '2px solid transparent',
        color: which === key ? '#0969da' : '#57606a',
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
          {all ? (
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
          ) : (
            <button
              type="button"
              onClick={loadAll}
              disabled={loadingAll}
              style={{
                marginTop: 12, border: '1px solid #d0d7de', borderRadius: 6,
                background: '#f6f8fa', padding: '4px 10px', fontSize: 13,
                cursor: loadingAll ? 'default' : 'pointer',
              }}
            >
              {loadingAll ? 'Loading…' : 'Show every email, including the ones that worked'}
            </button>
          )}
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

  useEffect(() => {
    if (!signedIn) return;
    api.me().then(setMe).catch((e) => {
      if (e?.status !== 401) logError('admin', e, 'load-me');
    });
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
  const actNow = today.act_now ?? [];
  const laterToday = (today.findings ?? []).filter((f) => f.severity !== 'high');

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
      </div>

      {/* Today — urgent. Separated from yesterday on purpose: mixing them
          buries a live problem under history. */}
      <div className="card" style={{
        borderLeft: `4px solid ${actNow.length ? SEVERITY.high.colour : '#1a7f37'}`,
      }}>
        <h3 style={{ margin: '0 0 4px' }}>
          {actNow.length ? `⚠️ ${actNow.length} thing${actNow.length > 1 ? 's' : ''} need attention now` : '✅ Nothing needs attention right now'}
        </h3>
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>Today, {today.date}</p>
        {actNow.map((f, i) => <Finding key={i} item={f} />)}
        {!actNow.length && !laterToday.length && (
          <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
            Jobs are on schedule and nothing has errored.
          </p>
        )}
        {laterToday.map((f, i) => <Finding key={`l${i}`} item={f} />)}
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
          : yesterday.findings.map((f, i) => <Finding key={i} item={f} />)}
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

      <Card title="Stories" subtitle={`Yesterday · model ${config?.story_model ?? '—'}`}>
        <Table
          head={['Status', 'Count']}
          empty="No stories were generated."
          rows={Object.entries(yesterday.stories?.by_status ?? {}).map(([k, v]) => [k, v])}
        />
        <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          Tokens in/out: {(yesterday.stories?.input_tokens ?? 0).toLocaleString()} /{' '}
          {(yesterday.stories?.output_tokens ?? 0).toLocaleString()}
          {yesterday.stories?.est_cost_usd != null
            && ` · estimated $${yesterday.stories.est_cost_usd.toFixed(4)}`}
        </p>
      </Card>

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
