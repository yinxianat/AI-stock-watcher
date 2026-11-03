import { useEffect, useRef, useState } from 'react';
import { Link, Navigate } from 'react-router-dom';
import { api, getToken } from '../lib/api.js';
import { logError } from '../lib/log.js';

// Root Cause — ask why something is happening in production.
//
// Data Lens answers "what does the database say"; this answers the question
// an admin actually arrives with. The two are separate pages because they are
// used at different moments: one when you know what to look at, one when you
// do not.
//
// The model picker is deliberately in front of the question rather than
// hidden in settings. "Why did the pipeline not run" is a lookup and "why
// does this story keep failing review" is reasoning, and only the person
// asking knows which — so the choice, and its price, sit where the choice is
// made.

const EXAMPLES = [
  'Why is there no AI article in this environment at all?',
  'Why did no recap email go out yesterday?',
  'Why are stories failing the reviewer?',
  'Is the scheduler running the jobs at the configured times?',
  'Which table is growing fastest and is its retention working?',
];

const CONFIDENCE = {
  high: { label: 'High confidence', colour: 'var(--ok)' },
  medium: { label: 'Medium confidence', colour: '#f5c451' },
  low: { label: 'Low confidence', colour: 'var(--danger)' },
};

// What the agent concluded. The fix comes before the evidence on purpose:
// the admin came here to know what to do, and the supporting detail is what
// they read only if they doubt it.
function Finding({ result }) {
  const c = CONFIDENCE[result.confidence] ?? CONFIDENCE.low;
  return (
    <div className="card rc-answer">
      <div className="rc-head">
        <strong>{result.cause}</strong>
        <span className="rc-confidence" style={{ color: c.colour }}>{c.label}</span>
      </div>
      <p className="rc-summary">{result.answer}</p>

      {result.fix?.length > 0 && (
        <>
          <h4 className="rc-h">What to do</h4>
          <ol className="rc-fix">
            {result.fix.map((f, i) => (
              <li key={i}>
                {f.step}
                <span className="muted"> — {f.where}</span>
              </li>
            ))}
          </ol>
        </>
      )}

      {result.verify && (
        <p className="rc-verify">
          <strong>Then check:</strong> {result.verify}
        </p>
      )}

      {result.evidence?.length > 0 && (
        <>
          <h4 className="rc-h">What says so</h4>
          <ul className="rc-evidence">
            {result.evidence.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </>
      )}

      <p className="muted rc-meta">
        {result.model} · {result.rounds} round{result.rounds === 1 ? '' : 's'} ·{' '}
        {result.usage?.est_cost} · {(result.duration_ms / 1000).toFixed(1)}s
        {result.files_read?.length > 0 && <> · read {result.files_read.join(', ')}</>}
        {result.query && <> · ran one read-only query</>}
      </p>
      <p className="muted rc-caveat">
        Written by an AI from the monitoring data, the logs and the source.
        Check anything you act on — especially before changing production.
      </p>
    </div>
  );
}

export default function RootCause() {
  const signedIn = !!getToken();
  const [models, setModels] = useState(null);
  const [model, setModel] = useState('');
  const [question, setQuestion] = useState('');
  const [asking, setAsking] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [denied, setDenied] = useState(null);
  const boxRef = useRef(null);

  useEffect(() => {
    if (!signedIn) return;
    api.rootCauseModels()
      .then((m) => {
        setModels(m);
        // Default to the cheapest usable one, which the backend sorts first.
        const first = m.models.find((x) => x.usable) ?? m.models[0];
        if (first) setModel(first.model);
      })
      .catch((e) => {
        setDenied(e?.status === 403
          ? 'This account is not an admin.'
          : (e.message || 'Could not load Root Cause.'));
        if (e?.status !== 403 && e?.status !== 401) logError('root-cause', e, 'models');
      });
  }, [signedIn]);

  const ask = (text = question) => {
    const q = text.trim();
    if (q.length < 5 || asking) return;
    setAsking(true);
    setError(null);
    setResult(null);
    api.rootCauseAsk(q, model || null)
      .then(setResult)
      .catch((e) => {
        const detail = e?.detail;
        setError({
          error: detail?.error ?? e?.userMessage ?? e?.message ?? 'No answer available.',
          hint: detail?.hint ?? null,
        });
        if (![401, 422, 503].includes(e?.status)) logError('root-cause', e, 'ask');
      })
      .finally(() => setAsking(false));
  };

  if (!signedIn) return <Navigate to="/" replace />;

  if (denied) {
    return (
      <div className="container">
        <div className="card">
          <h2>Root Cause</h2>
          <p className="flash warn">{denied}</p>
        </div>
      </div>
    );
  }

  if (!models) return <div className="container"><p>Loading…</p></div>;

  const chosen = models.models.find((m) => m.model === model);

  return (
    <div className="container">
      <div className="card">
        <h2 style={{ margin: 0 }}>Root Cause</h2>
        <p className="muted" style={{ margin: '4px 0 0' }}>
          Ask why something is happening in production. The agent reads the
          day's monitoring report, the recent logs and the source, then says
          what to do about it.
        </p>
        <p className="muted" style={{ margin: '4px 0 0', fontSize: 12 }}>
          It cannot change anything — any query it runs is read-only, and it
          only ever reads from <code>backend/app</code>. ·{' '}
          <Link to="/admin">monitoring</Link> · <Link to="/admin/data-lens">Data Lens</Link>
        </p>
      </div>

      {!models.enabled && (
        <div className="card">
          <p className="flash warn" style={{ margin: 0 }}>
            Root Cause is switched off. Set <code>ROOT_CAUSE_ENABLED=true</code> to use it.
          </p>
        </div>
      )}

      <div className="card">
        <label htmlFor="rc-q">Your question</label>
        <textarea
          id="rc-q"
          ref={boxRef}
          className="rc-input"
          rows={3}
          value={question}
          placeholder="Why is there no AI article in this environment at all?"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); ask(); }
          }}
        />

        <div className="rc-toolbar">
          <button
            type="button"
            onClick={() => ask()}
            disabled={asking || question.trim().length < 5 || !models.enabled}
          >
            {asking ? 'Investigating…' : 'Ask'}
          </button>
          <span className="muted rc-shortcut">⌘/Ctrl + Enter</span>

          <label htmlFor="rc-model" className="rc-model-label">Model</label>
          <select
            id="rc-model"
            className="rc-model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            {models.models.map((m) => (
              <option key={m.model} value={m.model} disabled={!m.usable}>
                {m.model} · {m.est_per_question}
                {m.usable ? '' : ' (no API key)'}
              </option>
            ))}
          </select>
        </div>

        {chosen && (
          <p className="muted rc-price">
            About {chosen.est_per_question} a question. {models.note}
          </p>
        )}

        <div className="rc-examples">
          {EXAMPLES.map((e) => (
            <button
              key={e}
              type="button"
              className="link-button rc-example"
              onClick={() => { setQuestion(e); ask(e); }}
              disabled={asking || !models.enabled}
            >
              {e}
            </button>
          ))}
        </div>
      </div>

      {asking && (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            Reading the day report, the logs and the delivery outcomes. It may
            ask for source files, which takes a second pass.
          </p>
        </div>
      )}

      {error && (
        <div className="card rc-error">
          <strong>No answer this time.</strong>
          <p className="rc-error-text">{error.error}</p>
          {error.hint && <p className="muted">{error.hint}</p>}
        </div>
      )}

      {result && <Finding result={result} />}
    </div>
  );
}
