import { useState } from 'react';
import { api } from '../lib/api.js';

export default function Login() {
  const [email, setEmail] = useState('');
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.requestLink(email.trim());
      setSent(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="container">
      <div className="card">
        <h2>Sign in to AI Stock Watcher</h2>
        <p className="muted">We'll email you a sign-in link. No password needed.</p>
        {sent ? (
          <div className="flash ok">
            Check your inbox for a link to sign in. It expires in 15 minutes.
          </div>
        ) : (
          <form onSubmit={submit}>
            {error && <div className="flash err">{error}</div>}
            <label>Email</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
            />
            <div style={{ marginTop: 16 }}>
              <button disabled={submitting}>{submitting ? 'Sending…' : 'Send link'}</button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
