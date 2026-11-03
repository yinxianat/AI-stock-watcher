import { useEffect, useState } from 'react';
import { Link, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { ErrorBoundary } from 'react-error-boundary';
import ErrorFallback from './components/ErrorFallback.jsx';
import { logRenderError } from './lib/log.js';
import { api, clearToken, getToken } from './lib/api.js';
import Login from './pages/Login.jsx';
import AuthCallback from './pages/AuthCallback.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Rules from './pages/Rules.jsx';
import Settings from './pages/Settings.jsx';
import ConfirmEmail from './pages/ConfirmEmail.jsx';
import Unsubscribe from './pages/Unsubscribe.jsx';
import Help from './pages/Help.jsx';
import Feedback from './pages/Feedback.jsx';
import MoneyIdeas from './pages/MoneyIdeas.jsx';
import Admin from './pages/Admin.jsx';
import DataLens from './pages/DataLens.jsx';
import RootCause from './pages/RootCause.jsx';

function RequireAuth({ children }) {
  return getToken() ? children : <Navigate to="/" replace />;
}

function Header() {
  const nav = useNavigate();
  const signedIn = !!getToken();
  // Only admins see the link. This is presentation only — the endpoints
  // re-check the role, so a client that lies to itself still gets 403.
  const [isAdmin, setIsAdmin] = useState(false);
  useEffect(() => {
    if (!signedIn) {
      setIsAdmin(false);
      return;
    }
    api.me().then((m) => setIsAdmin(!!m.is_admin)).catch(() => setIsAdmin(false));
  }, [signedIn]);
  return (
    <header className="app-header">
      <h1>🐵 Little Money Monkey</h1>
      <nav>
        {signedIn ? (
          <>
            <Link to="/dashboard">Dashboard</Link>
            <Link to="/money-ideas">Money Ideas</Link>
            <Link to="/settings">Settings</Link>
            {isAdmin && <Link to="/admin">Admin</Link>}
            {isAdmin && <Link to="/admin/data-lens">Data Lens</Link>}
            {isAdmin && <Link to="/admin/root-cause">Root Cause</Link>}
            <Link to="/help">Help</Link>
            <button
              type="button"
              className="link-button"
              onClick={() => {
                clearToken();
                nav('/');
              }}
            >
              Sign out
            </button>
          </>
        ) : (
          <>
            <Link to="/help">How it works</Link>
            <Link to="/">Sign in</Link>
          </>
        )}
      </nav>
    </header>
  );
}

export default function App() {
  const location = useLocation();
  return (
    <>
    <a href="#main-content" className="skip-link">
      Skip to main content
    </a>
      <Header />
      <main id="main-content" tabIndex={-1}>
      <ErrorBoundary
        fallbackRender={(props) => <ErrorFallback {...props} takeFocus />}
        resetKeys={[location.pathname]}
        onError={logRenderError}
        >
      
      <Routes>
        <Route path="/" element={<Login />} />
        <Route path="/auth/callback" element={<AuthCallback />} />
        <Route path="/auth/confirm-email" element={<ConfirmEmail />} />
        <Route path="/unsubscribe" element={<Unsubscribe />} />
        <Route path="/help" element={<Help />} />
        <Route path="/dashboard" element={<RequireAuth><Dashboard /></RequireAuth>} />
        <Route path="/rules/:tickerId" element={<RequireAuth><Rules /></RequireAuth>} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/money-ideas" element={<MoneyIdeas />} />
        <Route path="/admin" element={<RequireAuth><Admin /></RequireAuth>} />
        <Route path="/admin/data-lens" element={<RequireAuth><DataLens /></RequireAuth>} />
        <Route path="/admin/root-cause" element={<RequireAuth><RootCause /></RequireAuth>} />
        <Route path="/feedback" element={<Feedback />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      </ErrorBoundary>
      </main>
    </>
  );
}
