// Tiny fetch wrapper. Single place to attach the session token and
// translate non-2xx responses into thrown Errors.

import { ApiError, NetworkError, TimeoutError, errorFromResponse } from "../api/errors";

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const TOKEN_KEY = 'sw_session_token';
const DEFAULT_TIMEOUT_MS = 15000;


export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

async function request(path, { method = 'GET', body, auth = false, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (auth) {
    const t = getToken();
    if (!t) throw new ApiError("Not signed in", {status:401, path, method});
    headers.Authorization = `Bearer ${t}`;
  }
  // Abort on whichever comes first: the caller's signal (e.g. search-as-you-type)
  // or our timeout.
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  let res;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal
    });
  } catch(err){
    // Caller cancelled on purpose: pass it through so the UI can ignore it.
    if (err.name === "AbortError") throw err;
    if (err.name === "TimeoutError") throw new TimeoutError(err, {path, method, timeoutMs});
    // fetch only throws TypeError when no response arrived (offline, DNS, CORS).
    if (err instanceof TypeError) throw new NetworkError(err, {path, method});
    throw err;
  }
  
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (res.status === 401){
    clearToken();
    // notify the app so it can redirect
    throw new ApiError("Session expired", {status: 401, path, method});
  }
  if (!res.ok) {
    throw errorFromResponse(res, data, {path, method})
  }
  return data;
}

export const api = {
  // auth
  requestLink: (email) => request('/auth/request-link', { method: 'POST', body: { email } }),
  verify: (token) => request('/auth/verify', { method: 'POST', body: { token } }),
  me: () => request('/auth/me', { auth: true }),
  changeNotifyEmail: (new_email) =>
    request('/auth/notify-email', { method: 'POST', body: { new_email }, auth: true }),
  confirmNotifyEmail: (token) =>
    request('/auth/notify-email/confirm', { method: 'POST', body: { token } }),

  // tickers
  listTickers: () => request('/tickers'),
  searchTickers: (q, opts) => request(`/tickers/search?q=${encodeURIComponent(q)}`,opts),

  // watchlist
  listWatchlist: (opts) => request('/watchlist', { auth: true, ...opts }),
  addWatch: (payload) => request('/watchlist', { method: 'POST', body: payload, auth: true }),
  removeWatch: (id) => request(`/watchlist/${id}`, { method: 'DELETE', auth: true }),

  // rules
  listRules: () => request('/rules', { auth: true }),
  upsertRule: (payload) => request('/rules', { method: 'POST', body: payload, auth: true }),
  deleteRule: (id) => request(`/rules/${id}`, { method: 'DELETE', auth: true }),

  // trends
  myTrends: (opts) => request('/trends', { auth: true, ...opts }),
};
