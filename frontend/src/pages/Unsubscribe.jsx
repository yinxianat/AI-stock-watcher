import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../lib/api.js';

// Landing page for the "Stop all alert emails" link in alert emails. Works
// without signing in. Asks for a click instead of unsubscribing on page load,
// because email security scanners open links automatically.
export default function Unsubscribe() {
  const [params] = useSearchParams();
  const token = params.get('token');
  const [status, setStatus] = useState(token ? 'ready' : 'missing');

  async function confirm() {
    setStatus('working');
    try {
      await api.unsubscribe(token);
      setStatus('ok');
    } catch {
      setStatus('bad');
    }
  }

  return (
    <div className="container">
      <div className="card">
        <h2>Stop alert emails</h2>
        {(status === 'ready' || status === 'working') && (
          <>
            <p>
              You won't get any more stock-alert emails. Sign-in links will
              still be sent when you ask for one.
            </p>
            <button type="button" onClick={confirm} disabled={status === 'working'}>
              {status === 'working' ? 'Unsubscribing…' : 'Unsubscribe'}
            </button>
          </>
        )}
        {status === 'ok' && (
          <div className="flash ok" role="status">
            You're unsubscribed from alert emails. Changed your mind? Turn them
            back on in <Link to="/settings">Settings</Link>.
          </div>
        )}
        {status === 'bad' && (
          <div className="flash err" role="alert">
            This unsubscribe link is invalid or expired. You can turn alerts
            off in <Link to="/settings">Settings</Link> after signing in.
          </div>
        )}
        {status === 'missing' && (
          <div className="flash err" role="alert">Missing token in URL.</div>
        )}
      </div>
    </div>
  );
}
