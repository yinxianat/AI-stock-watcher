import { useEffect, useState } from 'react';
import { useSearchParams, Link } from 'react-router-dom';
import { api } from '../lib/api.js';

export default function Feedback() {
  const [params] = useSearchParams();
  const token = params.get('token');
  const [status, setStatus] = useState('loading');
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (!token) {
      setStatus('error');
      return;
    }
    api.rateFeedback(token)
      .then((r) => { setResult(r); setStatus('done'); })
      .catch(() => setStatus('error'));
  }, [token]);

  if (status === 'loading') {
    return (
      <div className="container">
        <div className="card" style={{ textAlign: 'center' }}>
          <p>Recording your feedback...</p>
        </div>
      </div>
    );
  }

  if (status === 'error') {
    return (
      <div className="container">
        <div className="card" style={{ textAlign: 'center' }}>
          <h2>Oops!</h2>
          <p>This feedback link is invalid or has expired.</p>
          <p><Link to="/">Go to Little Money Monkey</Link></p>
        </div>
      </div>
    );
  }

  const emoji = { like: '👍', neutral: '😐', dislike: '👎' }[result.rating] || '✅';
  const label = { like: 'Love it!', neutral: 'So-so', dislike: 'Not great' }[result.rating] || result.rating;

  return (
    <div className="container">
      <div className="card" style={{ textAlign: 'center' }}>
        <p style={{ fontSize: '48px', margin: '0' }}>{emoji}</p>
        <h2>Thanks for your feedback!</h2>
        <p>You rated this: <strong>{label}</strong></p>
        <p style={{ color: '#6e7781', fontSize: '14px' }}>
          Your feedback helps us make better content for your family.
        </p>
        <p><Link to="/money-ideas">See your Money Ideas</Link></p>
      </div>
    </div>
  );
}
