// Tiny fetch wrapper. Single place to attach the session token and
// translate non-2xx responses into thrown Errors.

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const TOKEN_KEY = 'sw_session_token';

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

async function request(path, { method = 'GET', body, auth = false } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (auth) {
    const t = getToken();
    if (!t) throw new Error('Not signed in');
    headers.Authorization = `Bearer ${t}`;
  }
  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.detail || res.statusText || 'Request failed';
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
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
  searchTickers: (q) => request(`/tickers/search?q=${encodeURIComponent(q)}`),

  // watchlist
  listWatchlist: () => request('/watchlist', { auth: true }),
  addWatch: (payload) => request('/watchlist', { method: 'POST', body: payload, auth: true }),
  removeWatch: (id) => request(`/watchlist/${id}`, { method: 'DELETE', auth: true }),

  // rules
  listRules: () => request('/rules', { auth: true }),
  upsertRule: (payload) => request('/rules', { method: 'POST', body: payload, auth: true }),
  deleteRule: (id) => request(`/rules/${id}`, { method: 'DELETE', auth: true }),

  // trends
  myTrends: () => request('/trends', { auth: true }),
};
