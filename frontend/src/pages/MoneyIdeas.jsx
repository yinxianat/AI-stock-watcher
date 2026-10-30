import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../lib/api.js';

export default function MoneyIdeas() {
  const [ideas, setIdeas] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.listMoneyIdeas()
      .then((d) => setIdeas(d.ideas))
      .catch((e) => setError(e.message || 'Failed to load'));
  }, []);

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

      {ideas.map((item, i) => (
        <div className="card" key={`${item.key}-${item.date}`}>
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
        </div>
      ))}

      {ideas.length > 0 && (
        <p style={{ textAlign: 'center', fontSize: '14px', color: '#6e7781', margin: '8px 0 24px' }}>
          {ideas.length} idea{ideas.length !== 1 ? 's' : ''} so far ·{' '}
          {ideas[ideas.length - 1].position} of {ideas[ideas.length - 1].total} in the curriculum
        </p>
      )}
    </div>
  );
}
