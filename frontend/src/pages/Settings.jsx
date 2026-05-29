import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

export default function Settings() {
  const [me, setMe] = useState(null);
  const [newEmail, setNewEmail] = useState('');
  const [flash, setFlash] = useState(null);

  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null));
  }, []);

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
    </div>
  );
}
