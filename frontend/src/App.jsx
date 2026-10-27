import { Link, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { ErrorBoundary } from 'react-error-boundary';
import ErrorFallback from './components/ErrorFallback.jsx';
import { logRenderError } from './lib/log.js';
import { clearToken, getToken } from './lib/api.js';
import Login from './pages/Login.jsx';
import AuthCallback from './pages/AuthCallback.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Rules from './pages/Rules.jsx';
import Settings from './pages/Settings.jsx';
import ConfirmEmail from './pages/ConfirmEmail.jsx';
import Unsubscribe from './pages/Unsubscribe.jsx';
import Help from './pages/Help.jsx';

function RequireAuth({ children }) {
  return getToken() ? children : <Navigate to="/" replace />;
}

function Header() {
  const nav = useNavigate();
  const signedIn = !!getToken();
  return (
    <header className="app-header">
      <h1>AI Stock Watcher</h1>
      <nav>
        {signedIn ? (
          <>
            <Link to="/dashboard">Dashboard</Link>
            <Link to="/settings">Settings</Link>
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
        <Route path="/settings" element={<RequireAuth><Settings /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      </ErrorBoundary>
      </main>
    </>
  );
}
