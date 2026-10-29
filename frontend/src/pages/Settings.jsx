import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { formatCountdown } from '../api/errors.js';
import { sendEmailRequest, useCooldown } from '../lib/emailSend.js';
import { suggestEmail } from '../lib/emailTypos.js';

// Matches the backend's per-user cooldown between confirmation emails.
const RESEND_COOLDOWN_S = 30;

export default function Settings() {
  const [me, setMe] = useState(null);
  const [newEmail, setNewEmail] = useState('');
  const [flash, setFlash] = useState(null);
  const [sending, setSending] = useState(false);
  // Seconds until "Send confirmation" can be used again (0 = ready).
  const [cooldown, startCooldown] = useCooldown();
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
      const outcome = await sendEmailRequest(() => api.changeNotifyEmail(target));
      setFlash(
        outcome === 'sent'
          ? {
              kind: 'ok',
              msg: `Confirmation email sent to ${target}. It usually arrives within a minute; click the link in it to start sending alerts there.`,
            }
          : {
              kind: 'warn',
              msg: `Your connection dropped before we heard back, so we can't be sure the confirmation email to ${target} went out. Check that inbox first; if nothing arrives, send it again.`,
            },
      );
      setNewEmail('');
      // Also after 'unsure': the server may have sent it and would refuse
      // another one this soon anyway.
      startCooldown(RESEND_COOLDOWN_S);
      api.me().then(setMe).catch(() => {});
    } catch (err) {
      setFlash({ kind: 'err', msg: err.message });
      // Wait as long as the server asked (could be minutes after the burst cap).
      if (err.status === 429) startCooldown(err.retryAfterSeconds ?? RESEND_COOLDOWN_S);
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  }

  const suggestion = suggestEmail(newEmail);

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
          <div className={`flash ${flash.kind}`} role={flash.kind === 'err' ? 'alert' : 'status'}>
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
            autoComplete="email"
            required
          />
          {suggestion && (
            <p className="hint">
              Did you mean{' '}
              <button type="button" className="link-button inline" onClick={() => setNewEmail(suggestion)}>
                {suggestion}
              </button>
              ?
            </p>
          )}
          <div style={{ marginTop: 12 }}>
            <button disabled={sending || cooldown > 0}>
              {sending
                ? 'Sending…'
                : cooldown > 0
                  ? `Resend in ${formatCountdown(cooldown)}`
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
