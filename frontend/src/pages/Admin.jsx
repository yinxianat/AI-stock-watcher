import { useEffect, useState } from 'react';
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

function Finding({ item }) {
  return (
    <div style={{ display: 'flex', gap: 10, padding: '10px 0', borderTop: '1px solid #eaeef2' }}>
      <Badge severity={item.severity} />
      <div style={{ minWidth: 0 }}>
        <p style={{ margin: 0, fontSize: 14 }}>{item.detail}</p>
        {item.remediation && (
          <p style={{ margin: '4px 0 0', fontSize: 13, color: '#57606a' }}>
            <strong>Fix:</strong> {item.remediation}
          </p>
        )}
        <code style={{ fontSize: 11, color: '#8c959f' }}>{item.code}</code>
      </div>
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

function timeOnly(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
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
          {timeOnly(watch.checked_at_et)} ET.
        </p>
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
          head={['Job', 'Expected ET', 'Status', 'Drift']}
          empty="No scheduled jobs for this day (weekend)."
          rows={(today.jobs ?? []).map((j) => [
            j.job_id,
            j.expected_et,
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

      <Card
        title="Story delivery"
        subtitle="Who received what yesterday, and why anyone missed out."
      >
        <Table
          head={['User', 'Reason', 'Watching', 'Days missed']}
          empty="Every eligible user got their recap."
          rows={(yesterday.delivery?.recap_missed ?? []).map((m) => [
            m.user,
            m.reason,
            m.watch_count ?? '—',
            m.missed_days_running ?? '—',
          ])}
        />
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
