// Helpers for buttons that ask the server to email the user (sign-in link,
// address confirmation).

import { useCallback, useEffect, useState } from 'react';
import { NetworkError, TimeoutError } from '../api/errors.js';

// Runs `send` and reports how it went:
//   'sent'   – the server accepted it.
//   'unsure' – the connection dropped before the reply, so we can't tell.
// If the user left the page while waiting (typically to open their mail app),
// a dropped connection counts as 'sent': the server got the request and the
// phone simply cut off the reply. Other errors (429, 400, …) are rethrown.
export async function sendEmailRequest(send) {
  let leftPage = document.visibilityState === 'hidden';
  const onChange = () => {
    if (document.visibilityState === 'hidden') leftPage = true;
  };
  document.addEventListener('visibilitychange', onChange);
  try {
    await send();
    return 'sent';
  } catch (err) {
    if (err instanceof NetworkError || err instanceof TimeoutError) {
      return leftPage ? 'sent' : 'unsure';
    }
    throw err;
  } finally {
    document.removeEventListener('visibilitychange', onChange);
  }
}

// Seconds left until the user may send again. Counts down from a deadline
// rather than ticking, so it's still right after the phone pauses the page.
export function useCooldown() {
  const [readyAt, setReadyAt] = useState(0);
  const [tick, setTick] = useState(0);
  const remaining = Math.max(0, Math.ceil((readyAt - Date.now()) / 1000));

  useEffect(() => {
    if (remaining <= 0) return;
    const id = setTimeout(() => setTick((t) => t + 1), 1000);
    return () => clearTimeout(id);
  }, [readyAt, tick]);

  const start = useCallback((seconds) => setReadyAt(Date.now() + seconds * 1000), []);
  return [remaining, start];
}
