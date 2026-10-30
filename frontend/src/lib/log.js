// Frontend errors go to the backend's /client-logs endpoint. From there,
// the normal server pipeline picks them up: DBLogHandler persists them
// to log_entries, EmailAlertHandler emails the admin (deduped per 15 min).
// So anything that lands here will show up in Railway's log_entries table
// and page the admin like any other server error.
//
// We keep a console.error as well — in dev or when offline, that's the
// only place these show up. The remote POST is fire-and-forget: a failure
// to report an error must never itself cause an error page.

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

async function report({ level, message, stack, source }) {
  if (import.meta.env.MODE === 'test') return;
  try {
    await fetch(`${BASE_URL}/client-logs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // keepalive lets the request outlive a page close, so errors on the
      // way out (navigation during a failed render) still get through.
      keepalive: true,
      body: JSON.stringify({
        level,
        message: String(message || 'unknown').slice(0, 500),
        stack: stack ? String(stack).slice(0, 4000) : undefined,
        url: typeof window !== 'undefined' ? window.location.href.slice(0, 500) : undefined,
        source,
      }),
    });
  } catch {
    // Reporting failed. Nothing to do — any throw here would be worse
    // than losing one log line.
  }
}

export function logError(source, err, extra) {
  // Keep developers' DevTools useful. In prod this still prints, which is
  // fine: the real monitoring is the server-side pipeline.
  // eslint-disable-next-line no-console
  console.error(`[${source}]`, err, extra);
  const message = extra ? `${extra}: ${err?.message || err}` : (err?.message || String(err));
  report({ level: 'error', message, stack: err?.stack, source });
}

export function logWarning(source, message, extra) {
  // eslint-disable-next-line no-console
  console.warn(`[${source}]`, message, extra);
  const text = extra ? `${message}: ${extra}` : String(message);
  report({ level: 'warning', message: text, source });
}

export function logRenderError(error, info) {
  // React error-boundary callback. The componentStack is the useful bit
  // for finding where a render blew up.
  // eslint-disable-next-line no-console
  console.error('Render Error', error);
  // eslint-disable-next-line no-console
  console.error('Component Stack:', info.componentStack);
  report({
    level: 'error',
    message: `[render] ${error?.message || error}`,
    stack: `${error?.stack || ''}\nComponent stack: ${info.componentStack || ''}`,
    source: 'render',
  });
}
