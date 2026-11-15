import { useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useReview } from "../context/ReviewContext";
import { Avatar, Logo } from "./ui";

function Status({ ok, label, detail }) {
  return <span className={`status ${ok ? "ok" : "off"}`} title={`${label}: ${detail}`}><i />{label}</span>;
}

export default function TopBar() {
  const { user, logout } = useAuth();
  const { health, activeIncidents } = useReview();
  const [theme, setTheme] = useState(document.documentElement.dataset.theme || "dark");
  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("warren_theme", next);
    setTheme(next);
  };
  const navigate = useNavigate();
  return (
    <header className="topbar">
      <div className="brand" onClick={() => navigate("/")}><Logo /><span>warren</span></div>
      <nav>
        <NavLink to="/" end>Review board</NavLink>
        <NavLink to="/incidents">CI / CD {activeIncidents > 0 && <span className="navbadge">{activeIncidents}</span>}</NavLink>
        <NavLink to="/security">Security</NavLink>
        <NavLink to="/repos">Repositories</NavLink>
      </nav>
      <div className="topbar-right">
        {health && (
          <div className="statuses">
            <Status ok={health.llm?.ok} label="LLM" detail={health.llm?.ok ? `OmniRoute · ${health.llm.model}` : "OmniRoute not reachable"} />
            <Status ok={health.sandbox} label="E2B" detail={health.sandbox ? "sandbox ready" : "set E2B_API_KEY"} />
            <Status ok label="Vectors" detail={`Chroma · ${health.embeddings} embeddings`} />
          </div>
        )}
        <button className="btn ghost small" onClick={toggleTheme} title="Switch theme">{theme === "dark" ? "Light theme" : "Dark theme"}</button>
        <Avatar src={user.avatar_url} name={user.login} size={28} />
        <span className="muted small">{user.login}</span>
        <button className="btn ghost small" onClick={() => { logout(); navigate("/login"); }}>Sign out</button>
      </div>
    </header>
  );
}
