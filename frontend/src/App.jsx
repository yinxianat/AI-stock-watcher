import { Link, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { clearToken, getToken } from './lib/api.js';
import Login from './pages/Login.jsx';
import AuthCallback from './pages/AuthCallback.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Rules from './pages/Rules.jsx';
import Settings from './pages/Settings.jsx';
import ConfirmEmail from './pages/ConfirmEmail.jsx';

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
            <a
              href="#"
              onClick={(e) => {
                e.preventDefault();
                clearToken();
                nav('/');
              }}
            >
              Sign out
            </a>
          </>
        ) : (
          <Link to="/">Sign in</Link>
        )}
      </nav>
    </header>
  );
}

export default function App() {
  return (
    <>
      <Header />
      <Routes>
        <Route path="/" element={<Login />} />
        <Route path="/auth/callback" element={<AuthCallback />} />
        <Route path="/auth/confirm-email" element={<ConfirmEmail />} />
        <Route path="/dashboard" element={<RequireAuth><Dashboard /></RequireAuth>} />
        <Route path="/rules/:tickerId" element={<RequireAuth><Rules /></RequireAuth>} />
        <Route path="/settings" element={<RequireAuth><Settings /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </>
  );
}
