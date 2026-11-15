import { useState } from "react";
import { Link } from "react-router-dom";
import { api, waitForJob } from "../api/client";
import PRCard from "../components/PRCard";
import { Icon, Spinner } from "../components/ui";
import { useAuth } from "../context/AuthContext";
import { useReview } from "../context/ReviewContext";

const PULL_VIEWS = [["all", "All reviews"], ["needs_my_review", "Needs my review"], ["authored_by_me", "Authored by me"], ["safe_to_merge", "Safe to merge"]];
const SAVED_VIEWS = [["ci_failing", "CI failing"], ["security_watch", "Security watch"], ["release_train", "Release train"], ["agent_created", "Agent-created PRs"]];

export default function Board() {
  const { user } = useAuth();
  const { board, repos, view, setView, repoId, setRepoId, query, setQuery, loading, error, loadBoard, loadRepos } = useReview();
  const [syncing, setSyncing] = useState(false);
  const enabled = repos.filter((r) => r.enabled);

  const sync = async () => {
    setSyncing(true);
    try {
      await Promise.all(enabled.map(async (r) => waitForJob((await api.syncRepo(r.id)).job_id)));
      await Promise.all([loadBoard(), loadRepos()]);
    } catch (e) {
      alert(e.message);
    } finally {
      setSyncing(false);
    }
  };

  const NavItem = ([key, label]) => (
    <button key={key} className={`navitem ${view === key ? "active" : ""}`} onClick={() => setView(key)}>
      <span>{label}</span><span className="count">{board.counts[key] ?? 0}</span>
    </button>
  );

  return (
    <div className="board-page">
      <div className="toolbar">
        <div className="search">
          <Icon name="search" size={16} />
          <input placeholder="Search pull requests" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="toolbar-right">
          {enabled.length > 1 && (
            <select value={repoId} onChange={(e) => setRepoId(e.target.value)}>
              <option value="">All repositories</option>
              {enabled.map((r) => <option key={r.id} value={r.id}>{r.full_name}</option>)}
            </select>
          )}
          <button className="btn small" onClick={sync} disabled={syncing || user.demo || enabled.length === 0}>
            {syncing ? <Spinner /> : <Icon name="refresh" size={14} />} Sync PRs
          </button>
        </div>
      </div>

      <div className="board-body">
        <aside className="panel side">
          <div className="eyebrow">Pull requests</div>
          {PULL_VIEWS.map(NavItem)}
          <div className="eyebrow gap">Saved views</div>
          {SAVED_VIEWS.map(NavItem)}
        </aside>

        {error && <div className="alert wide">{error}</div>}
        {enabled.length === 0 && !loading ? (
          <div className="empty">
            <h2>No repositories connected yet</h2>
            <p className="muted">Connect a repository and Warren will review its pull requests and rank them here.</p>
            <Link className="btn primary" to="/repos">Connect a repository</Link>
          </div>
        ) : (
          <div className="columns">
            {Object.entries(board.columns).map(([key, col]) => (
              <section key={key} className="column">
                <header>
                  <div><h2>{key}</h2><span className="muted small">{col.label}</span></div>
                  <span className="countbox">{col.items.length}</span>
                </header>
                <div className="cards">
                  {col.items.map((pr) => <PRCard key={pr.id} pr={pr} onChange={loadBoard} readOnly={user.demo} />)}
                  {col.items.length === 0 && <div className="muted small emptycol">{loading ? "Loading…" : "Nothing here"}</div>}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
