import { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import { formatCountdown } from '../api/errors.js';
import { sendEmailRequest, useCooldown } from '../lib/emailSend.js';
import { suggestEmail } from '../lib/emailTypos.js';

// Matches the backend's default wait between sign-in emails (EMAIL_RESEND_COOLDOWN_SECONDS).
const RESEND_COOLDOWN_S = 30;

export default function Login() {
  const [email, setEmail] = useState('');
  // Address the last link went to, and whether we know it was sent
  // ('sent') or the connection dropped before the reply ('unsure').
  const [sentTo, setSentTo] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  // Seconds until another link can be requested (0 = ready).
  const [cooldown, startCooldown] = useCooldown();
  // A ref (not state) so a fast double-click can't send two requests before
  // React re-renders with the button disabled.
  const inFlight = useRef(false);
  const suggestion = suggestEmail(email);

  async function send() {
    if (inFlight.current || cooldown > 0) return;
    inFlight.current = true;
    const target = email.trim();
    setError(null);
    setSubmitting(true);
    try {
      setOutcome(await sendEmailRequest(() => api.requestLink(target)));
      setSentTo(target);
      // Also after 'unsure': the server may have sent it and would refuse
      // another one this soon anyway.
      startCooldown(RESEND_COOLDOWN_S);
    } catch (err) {
      setError(err.message);
      // Wait as long as the server asked (could be minutes after the burst cap).
      if (err.status === 429) startCooldown(err.retryAfterSeconds ?? RESEND_COOLDOWN_S);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  function submit(e) {
    e.preventDefault();
    send();
  }

  function changeEmail() {
    setSentTo(null);
    setOutcome(null);
    setError(null);
    // The server's limit is per address, so a new address needn't wait.
    startCooldown(0);
  }

  const waitLabel = cooldown > 0 ? `Send again in ${formatCountdown(cooldown)}` : null;

  return (
    <div className="container">
      <div className="card">
        <h2>🐵 Sign in to Little Money Monkey</h2>
        {sentTo ? (
          <>
            {outcome === 'sent' ? (
              <div className="flash ok" role="status">
                Sign-in link sent to <strong>{sentTo}</strong>.
              </div>
            ) : (
              <div className="flash warn" role="status">
                Your connection dropped before we heard back, so we can't be sure
                the link to <strong>{sentTo}</strong> went out. Check your inbox
                first; if nothing arrives, send it again.
              </div>
            )}
            <p>It usually arrives within a minute. The link works for 15 minutes.</p>
            {error && <div className="flash err" role="alert">{error}</div>}
            <p className="muted">
              {cooldown > 0 ? (
                <>
                  Not there yet? Check your Spam or Promotions folder. You can send
                  another link in {formatCountdown(cooldown)}.
                </>
              ) : (
                <>
                  Still nothing? Make sure the address above is right, or{' '}
                  <button
                    type="button"
                    className="link-button inline"
                    onClick={send}
                    disabled={submitting}
                  >
                    {submitting ? 'sending…' : 'send it again'}
                  </button>
                  .
                </>
              )}
            </p>
            <p>
              <button type="button" className="link-button inline" onClick={changeEmail}>
                Use a different email
              </button>
            </p>
          </>
        ) : (
          <>
            <p className="muted">
              We'll email you a sign-in link. No password needed.{' '}
              <Link to="/help">New here? See how it works.</Link>
            </p>
            <form onSubmit={submit}>
              {error && <div className="flash err" role="alert">{error}</div>}
              <label htmlFor="login-email">Email</label>
              <input
                id="login-email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
              />
              {suggestion && (
                <p className="hint">
                  Did you mean{' '}
                  <button type="button" className="link-button inline" onClick={() => setEmail(suggestion)}>
                    {suggestion}
                  </button>
                  ?
                </p>
              )}
              <div style={{ marginTop: 16 }}>
                <button disabled={submitting || cooldown > 0}>
                  {submitting ? 'Sending…' : waitLabel ?? 'Send link'}
                </button>
              </div>
            </form>
          </>
        )}
      </div>
    </div>
  );
}
