import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

export default function Settings() {
  const [me, setMe] = useState(null);
  const [newEmail, setNewEmail] = useState('');
  const [flash, setFlash] = useState(null);
  // null while loading or if it failed to load; the toggle is hidden then.
  const [alertsEnabled, setAlertsEnabled] = useState(null);
  const [savingAlerts, setSavingAlerts] = useState(false);
  const [alertsFlash, setAlertsFlash] = useState(null);

  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null));
    api
      .getEmailPreferences()
      .then((p) => setAlertsEnabled(p.alerts_enabled))
      .catch(() => setAlertsEnabled(null));
  }, []);

  async function toggleAlerts(e) {
    const enabled = e.target.checked;
    setSavingAlerts(true);
    setAlertsFlash(null);
    try {
      const p = await api.setAlertsEnabled(enabled);
      setAlertsEnabled(p.alerts_enabled);
    } catch (err) {
      setAlertsFlash(err.message);
    } finally {
      setSavingAlerts(false);
    }
  }

  async function submit(e) {
    e.preventDefault();
    setFlash(null);
    try {
      await api.changeNotifyEmail(newEmail.trim());
      setFlash({
        kind: 'ok',
        msg: 'Confirmation email sent. Click the link in your inbox to activate the new address.',
      });
      setNewEmail('');
      const refreshed = await api.me();
      setMe(refreshed);
    } catch (err) {
      setFlash({ kind: 'err', msg: err.message });
    }
  }

  if (!me) return <div className="container"><p>Loading…</p></div>;

  return (
    <div className="container">
      <div className="card">
        <h2>Settings</h2>
        <p>
          <strong>Login email:</strong> {me.email}
        </p>
        <p>
          <strong>Notification email:</strong> {me.notify_email}{' '}
          {me.notify_email_confirmed ? (
            <span className="muted">(confirmed)</span>
          ) : (
            <span style={{ color: 'var(--danger)' }}>(pending confirmation)</span>
          )}
        </p>
        {flash && <div className={`flash ${flash.kind}`}>{flash.msg}</div>}
        <form onSubmit={submit}>
          <label>Change notification email</label>
          <input
            type="email"
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            placeholder="other@example.com"
            required
          />
          <div style={{ marginTop: 12 }}>
            <button>Send confirmation</button>
          </div>
        </form>
      </div>

      {alertsEnabled !== null && (
        <div className="card">
          <h3>Alert emails</h3>
          <label className="toggle">
            <input
              type="checkbox"
              checked={alertsEnabled}
              onChange={toggleAlerts}
              disabled={savingAlerts}
            />
            <span>Email me when my stock alerts are triggered</span>
          </label>
          <p className="muted">
            {alertsEnabled
              ? 'Alerts are on. Each stock\'s rules decide when you get an email.'
              : 'Alerts are off. Your rules are kept, but no alert emails will be sent until you turn this back on.'}{' '}
            Sign-in links are always sent.
          </p>
          {alertsFlash && <div className="flash err">{alertsFlash}</div>}
        </div>
      )}
    </div>
  );
}
