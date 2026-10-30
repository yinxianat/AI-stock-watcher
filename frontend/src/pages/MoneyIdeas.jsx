import { useEffect, useState } from 'react';
import { Link, Navigate, useSearchParams } from 'react-router-dom';
import { api, getToken } from '../lib/api.js';
import { logError } from '../lib/log.js';

export default function MoneyIdeas() {
  const [params] = useSearchParams();
  const token = params.get('token');
  const signedIn = !!getToken();
  const [ideas, setIdeas] = useState(null);
  const [feedback, setFeedback] = useState({});
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!token && !signedIn) return;
    const load = token ? api.listMoneyIdeasByToken(token) : api.listMoneyIdeas();
    load
      .then((d) => setIdeas(d.ideas))
      .catch((e) => {
        setError(e.message || 'Failed to load');
        // 400 on a bad/expired token is a user outcome, not a bug.
        if (e?.status !== 400) logError('money-ideas', e, 'load');
      });
    // Ratings need a session; arriving from an email link we skip them.
    if (!signedIn) return;
    api.listFeedback('money_idea')
      .then((d) => {
        const map = {};
        d.feedback.forEach((f) => { map[f.content_id] = f.rating; });
        setFeedback(map);
      })
      .catch((e) => logError('money-ideas', e, 'load-feedback'));
  }, [token, signedIn]);

  if (!token && !signedIn) return <Navigate to="/" replace />;

  const handleRate = (contentId, rating) => {
    const previous = feedback[contentId];
    setFeedback((prev) => ({ ...prev, [contentId]: rating }));
    api.updateFeedback('money_idea', contentId, rating).catch((e) => {
      // Roll back the optimistic update and report the failure.
      setFeedback((prev) => ({ ...prev, [contentId]: previous }));
      logError('money-ideas', e, `rate=${rating}`);
    });
  };

  if (error) return <div className="container"><p className="flash warn">{error}</p></div>;
  if (!ideas) return <div className="container"><p>Loading…</p></div>;

  return (
    <div className="container">
      <div className="card">
        <h2>Money Ideas</h2>
        <p>
          Every daily recap teaches one small money idea, so you and your kids
          build a foundation of financial awareness together. Here's every idea
          you've received so far.
        </p>
        {ideas.length === 0 && (
          <p className="flash info">
            No money ideas yet. You'll get your first one with your next{' '}
            <Link to="/dashboard">daily recap</Link>.
          </p>
        )}
      </div>

      {ideas.map((item, i) => {
        const contentId = `${item.key}:${item.date}`;
        const current = feedback[contentId];
        return (
          <div className="card" key={contentId}>
            <p style={{ margin: '0 0 4px', fontSize: '12px', letterSpacing: '.04em', color: '#6e7781' }}>
              🌱 {item.position} of {item.total} · {new Date(item.date + 'T00:00:00').toLocaleDateString(undefined, {
                weekday: 'short', month: 'short', day: 'numeric', year: 'numeric'
              })}
            </p>
            <h3 style={{ margin: '0 0 8px' }}>{item.title}</h3>
            {item.versions.map((v) => (
              <details key={v.age_group} open={i === 0}>
                <summary>Ages {v.age_group}</summary>
                <p>{v.idea}</p>
                <p><strong>🗣️ Ask your kid:</strong> {v.question}</p>
              </details>
            ))}
            {signedIn && (
            <div style={{ display: 'flex', gap: '8px', marginTop: '10px', alignItems: 'center' }}>
              <span style={{ fontSize: '13px', color: '#6e7781' }}>Was this helpful?</span>
              {[
                { rating: 'like', emoji: '👍', label: 'Love it' },
                { rating: 'neutral', emoji: '😐', label: 'So-so' },
                { rating: 'dislike', emoji: '👎', label: 'Not great' },
              ].map(({ rating, emoji, label }) => (
                <button
                  key={rating}
                  type="button"
                  onClick={() => handleRate(contentId, rating)}
                  style={{
                    padding: '4px 10px', borderRadius: '6px', border: '1px solid #d0d7de',
                    background: current === rating ? '#dafbe1' : '#fff', cursor: 'pointer',
                    fontSize: '13px', fontWeight: current === rating ? 'bold' : 'normal',
                  }}
                  title={label}
                >
                  {emoji}
                </button>
              ))}
            </div>
            )}
          </div>
        );
      })}

      {ideas.length > 0 && (
        <p style={{ textAlign: 'center', fontSize: '14px', color: '#6e7781', margin: '8px 0 24px' }}>
          {ideas.length} idea{ideas.length !== 1 ? 's' : ''} so far ·{' '}
          {ideas[ideas.length - 1].position} of {ideas[ideas.length - 1].total} in the curriculum
        </p>
      )}
    </div>
  );
}
