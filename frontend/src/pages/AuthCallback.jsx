import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, setToken } from '../lib/api.js';

export default function AuthCallback() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const [error, setError] = useState(null);

  useEffect(() => {
    const token = params.get('token');
    if (!token) {
      setError('No token in URL');
      return;
    }
    (async () => {
      try {
        const res = await api.verify(token);
        setToken(res.session_token);
        nav('/dashboard');
      } catch (err) {
        setError(err.message);
      }
    })();
  }, [params, nav]);

  return (
    <div className="container">
      <div className="card">
        {error ? (
          <div className="flash err">
            Could not sign you in: {error}. <a href="/">Back to sign-in</a>.
          </div>
        ) : (
          <p>Signing you in…</p>
        )}
      </div>
    </div>
  );
}
