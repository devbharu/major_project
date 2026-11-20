import { useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { api, API_BASE } from "../api/client";
import { Icon, Logo } from "../components/ui";
import { useAuth } from "../context/AuthContext";

export default function Login() {
  const { user, config, signInWithToken } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [error, setError] = useState(params.get("error") || "");
  const [pat, setPat] = useState("");
  const [showPat, setShowPat] = useState(false);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to="/" replace />;

  const run = async (fn) => {
    setBusy(true);
    setError("");
    try {
      const { token } = await fn();
      await signInWithToken(token);
      navigate("/");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="login-card">
        <Logo size={44} />
        <h1>Cut review time <em>and</em> bugs in half.</h1>
        <p className="muted">Warren reviews every pull request, prioritises your queue, explains the blast radius, and fixes what it finds — verified in a sandbox.</p>

        <a className={`btn primary big ${config.github_oauth ? "" : "disabled"}`} href={config.github_oauth ? `${API_BASE}/api/auth/github/login` : undefined}
           onClick={(e) => !config.github_oauth && e.preventDefault()}>
          <Icon name="github" size={18} /> Continue with GitHub
        </a>
        {!config.github_oauth && <p className="small muted">GitHub OAuth isn’t configured yet (set GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET). Use a token below or the demo.</p>}
        {config.demo && <button className="btn big" disabled={busy} onClick={() => run(api.demoLogin)}>Try the demo (read-only)</button>}

        <button className="linkbtn" onClick={() => setShowPat(!showPat)}>Sign in with a personal access token</button>
        {showPat && (
          <div className="patbox">
            <input type="password" placeholder="ghp_… (scope: repo)" value={pat} onChange={(e) => setPat(e.target.value)} />
            <button className="btn" disabled={busy || !pat} onClick={() => run(() => api.tokenLogin(pat))}>Sign in</button>
          </div>
        )}
        {error && <div className="alert">{error}</div>}
      </div>
    </div>
  );
}
