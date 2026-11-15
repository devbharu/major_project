import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

// GitHub → Flask → here:  /auth/callback#token=<session jwt>
export default function AuthCallback() {
  const { signInWithToken } = useAuth();
  const navigate = useNavigate();
  useEffect(() => {
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
    window.history.replaceState(null, "", window.location.pathname);   // never leave the session token in history
    if (!token) return navigate("/login?error=Missing+session+token", { replace: true });
    signInWithToken(token).then(() => navigate("/", { replace: true }));
  }, [navigate, signInWithToken]);
  return <div className="center muted">Signing you in…</div>;
}
