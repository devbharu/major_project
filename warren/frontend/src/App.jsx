import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import TopBar from "./components/TopBar";
import { useAuth } from "./context/AuthContext";
import AuthCallback from "./pages/AuthCallback";
import Board from "./pages/Board";
import Login from "./pages/Login";

const Incidents = lazy(() => import("./pages/Incidents"));       // route-level code splitting keeps the first load small
const PRDetail = lazy(() => import("./pages/PRDetail"));
const Repos = lazy(() => import("./pages/Repos"));
const Security = lazy(() => import("./pages/Security"));

function Protected({ children }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="center muted">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  return (
    <div className="shell">
      <TopBar />
      <Suspense fallback={<div className="center muted">Loading…</div>}>{children}</Suspense>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route path="/" element={<Protected><Board /></Protected>} />
      <Route path="/pr/:id" element={<Protected><PRDetail /></Protected>} />
      <Route path="/incidents" element={<Protected><Incidents /></Protected>} />
      <Route path="/incidents/:id" element={<Protected><Incidents /></Protected>} />
      <Route path="/security" element={<Protected><Security /></Protected>} />
      <Route path="/repos" element={<Protected><Repos /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
