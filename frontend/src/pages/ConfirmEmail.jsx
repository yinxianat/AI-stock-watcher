import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../lib/api.js';

export default function ConfirmEmail() {
  const [params] = useSearchParams();
  const [status, setStatus] = useState('working');

  useEffect(() => {
    const token = params.get('token');
    if (!token) {
      setStatus('missing');
      return;
    }
    api
      .confirmNotifyEmail(token)
      .then(() => setStatus('ok'))
      .catch(() => setStatus('bad'));
  }, [params]);

  return (
    <div className="container">
      <div className="card">
        {status === 'working' && <p>Confirming…</p>}
        {status === 'ok' && (
          <div className="flash ok">
            Notification email confirmed. You'll receive alerts at this address from now on.
          </div>
        )}
        {status === 'bad' && (
          <div className="flash err">This confirmation link is invalid or expired.</div>
        )}
        {status === 'missing' && (
          <div className="flash err">Missing token in URL.</div>
        )}
      </div>
    </div>
  );
}
