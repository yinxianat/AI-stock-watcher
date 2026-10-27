import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import { formatCountdown } from '../api/errors.js';

// Matches the backend's default wait between sign-in emails (EMAIL_RESEND_COOLDOWN_SECONDS).
const RESEND_COOLDOWN_S = 30;

export default function Login() {
  const [email, setEmail] = useState('');
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  // Seconds until another link can be requested (0 = ready).
  const [cooldown, setCooldown] = useState(0);
  // A ref (not state) so a fast double-click can't send two requests before
  // React re-renders with the button disabled.
  const inFlight = useRef(false);

  useEffect(() => {
    if (cooldown <= 0) return;
    const id = setTimeout(() => setCooldown((s) => s - 1), 1000);
    return () => clearTimeout(id);
  }, [cooldown]);

  async function send() {
    if (inFlight.current || cooldown > 0) return;
    inFlight.current = true;
    setError(null);
    setSubmitting(true);
    try {
      await api.requestLink(email.trim());
      setSent(true);
      setCooldown(RESEND_COOLDOWN_S);
    } catch (err) {
      setError(err.message);
      // Wait as long as the server asked (could be minutes after the hourly cap).
      if (err.status === 429) setCooldown(err.retryAfterSeconds ?? RESEND_COOLDOWN_S);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  function submit(e) {
    e.preventDefault();
    send();
  }

  const waitLabel = cooldown > 0 ? `Send again in ${formatCountdown(cooldown)}` : null;

  return (
    <div className="container">
      <div className="card">
        <h2>Sign in to AI Stock Watcher</h2>
        <p className="muted">
          We'll email you a sign-in link. No password needed.{' '}
          <Link to="/help">New here? See how it works.</Link>
        </p>
        {sent ? (
          <>
            <div className="flash ok" role="status">
              Check your inbox for a link to sign in. It expires in 15 minutes.
            </div>
            {error && <div className="flash err" role="alert">{error}</div>}
            <p className="muted">
              Didn't get it? Check your spam folder, or{' '}
              <button
                type="button"
                className="link-button inline"
                onClick={send}
                disabled={submitting || cooldown > 0}
              >
                {submitting ? 'sending…' : waitLabel ? waitLabel.toLowerCase() : 'send it again'}
              </button>
              .
            </p>
          </>
        ) : (
          <form onSubmit={submit}>
            {error && <div className="flash err" role="alert">{error}</div>}
            <label htmlFor="login-email">Email</label>
            <input
              id="login-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
            />
            <div style={{ marginTop: 16 }}>
              <button disabled={submitting || cooldown > 0}>
                {submitting ? 'Sending…' : waitLabel ?? 'Send link'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
