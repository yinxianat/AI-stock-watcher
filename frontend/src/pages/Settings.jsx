import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';

// Matches the backend's per-user cooldown between confirmation emails.
const RESEND_COOLDOWN_S = 60;

export default function Settings() {
  const [me, setMe] = useState(null);
  const [newEmail, setNewEmail] = useState('');
  const [flash, setFlash] = useState(null);
  const [sending, setSending] = useState(false);
  // Seconds until "Send confirmation" can be used again (0 = ready).
  const [cooldown, setCooldown] = useState(0);
  // Blocks a fast double-click before React re-renders the disabled button.
  const inFlight = useRef(false);
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

  useEffect(() => {
    if (cooldown <= 0) return;
    const id = setTimeout(() => setCooldown((s) => s - 1), 1000);
    return () => clearTimeout(id);
  }, [cooldown]);

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
    // Ignore extra clicks (or Enter presses) while a request is in flight
    // or during the cooldown.
    if (inFlight.current || cooldown > 0) return;
    const target = newEmail.trim().toLowerCase();
    if (me.notify_email_confirmed && target === me.notify_email.toLowerCase()) {
      setFlash({ kind: 'warn', msg: "That's already your notification email. Enter a different address to change it." });
      return;
    }
    inFlight.current = true;
    setFlash(null);
    setSending(true);
    try {
      await api.changeNotifyEmail(target);
      setFlash({
        kind: 'ok',
        msg: 'Confirmation email sent. Click the link in your inbox to activate the new address.',
      });
      setNewEmail('');
      setCooldown(RESEND_COOLDOWN_S);
      const refreshed = await api.me();
      setMe(refreshed);
    } catch (err) {
      setFlash({ kind: 'err', msg: err.message });
      if (err.status === 429) setCooldown(RESEND_COOLDOWN_S);
    } finally {
      inFlight.current = false;
      setSending(false);
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
        {flash && (
          <div className={`flash ${flash.kind}`} role={flash.kind === 'ok' ? 'status' : 'alert'}>
            {flash.msg}
          </div>
        )}
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
            <button disabled={sending || cooldown > 0}>
              {sending
                ? 'Sending…'
                : cooldown > 0
                  ? `Resend in ${cooldown}s`
                  : 'Send confirmation'}
            </button>
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
