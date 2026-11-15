import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Icon, Pill, Spinner, timeAgo } from "../components/ui";
import { useAuth } from "../context/AuthContext";
import { useReview } from "../context/ReviewContext";

const CI_MODES = [["off", "Diagnose only", "Explain failures, never change code"], ["approval", "Ask first", "Prepare fixes, wait for your OK"], ["auto", "Autonomous", "Apply safe, verified fixes; ask when risky"]];
const PROFILES = [["quiet", "Quiet", "Only critical & major"], ["chill", "Chill", "Adds minor issues"], ["assertive", "Assertive", "Everything, incl. nitpicks"]];

function Toggle({ on, onChange, label, hint }) {
  return (
    <label className="toggle">
      <input type="checkbox" checked={on} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch" /><span><b>{label}</b><span className="muted small"> {hint}</span></span>
    </label>
  );
}

function SettingsPanel({ repo, readOnly, onSaved }) {
  const [s, setS] = useState(repo.settings);
  const [ignore, setIgnore] = useState(repo.settings.ignore_paths.join("\n"));
  const [learnings, setLearnings] = useState([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => { api.learnings(repo.id).then((d) => setLearnings(d.learnings)).catch(() => {}); }, [repo.id]);

  const save = async () => {
    setSaving(true);
    try {
      const res = await api.saveSettings(repo.id, { ...s, ignore_paths: ignore.split("\n").map((x) => x.trim()).filter(Boolean) });
      setS(res.repo.settings);                      // instructions come back rewritten as clean, professional guidance
      onSaved();
    } catch (e) {
      alert(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="settings">
      <div className="eyebrow">Review profile</div>
      <div className="profiles">
        {PROFILES.map(([k, label, hint]) => (
          <button key={k} className={s.profile === k ? "on" : ""} onClick={() => setS({ ...s, profile: k })}><b>{label}</b><span className="muted small">{hint}</span></button>
        ))}
      </div>
      <div className="eyebrow gap">CI / CD doctor</div>
      <div className="profiles">
        {CI_MODES.map(([k, label, hint]) => (
          <button key={k} className={s.ci_mode === k ? "on" : ""} onClick={() => setS({ ...s, ci_mode: k })}><b>{label}</b><span className="muted small">{hint}</span></button>
        ))}
      </div>
      <div className="eyebrow gap">Behaviour</div>
      <Toggle on={s.auto_review} onChange={(v) => setS({ ...s, auto_review: v })} label="Auto-review" hint="review every new PR and new commit" />
      <Toggle on={s.auto_fix} onChange={(v) => setS({ ...s, auto_fix: v })} label="Auto-fix" hint="commit verified fixes after each review" />
      <Toggle on={s.post_comments} onChange={(v) => setS({ ...s, post_comments: v })} label="Post to GitHub" hint="off = results stay in the dashboard" />
      <Toggle on={s.commit_status} onChange={(v) => setS({ ...s, commit_status: v })} label="Pre-merge status" hint="publish warren/pre-merge so branch protection can require it" />
      <div className="eyebrow gap">Review instructions <span className="muted small">(Warren rewrites them into clear, professional guidance when you save)</span></div>
      <textarea rows={4} placeholder="e.g. Handlers own HTTP mapping. Flag any raw SQL. Prefer early returns." value={s.instructions} onChange={(e) => setS({ ...s, instructions: e.target.value })} />
      <div className="eyebrow gap">Ignored paths (one glob per line)</div>
      <textarea rows={4} className="mono" value={ignore} onChange={(e) => setIgnore(e.target.value)} />
      <button className="btn primary small" onClick={save} disabled={saving || readOnly}>{saving ? <Spinner /> : null} Save settings</button>

      <div className="eyebrow gap">Learnings ({learnings.length})</div>
      {learnings.length === 0 && <p className="muted small">Reply to Warren in a PR (“we always …”) and it will remember the convention.</p>}
      {learnings.map((l) => (
        <div key={l.id} className="learning">
          <div>{l.text}<div className="muted small">{l.path_hint && <span className="mono">{l.path_hint} · </span>}from #{l.source_pr} by {l.created_by}</div></div>
          <button className="iconbtn" title="Forget" disabled={readOnly} onClick={async () => { await api.deleteLearning(repo.id, l.id); setLearnings(learnings.filter((x) => x.id !== l.id)); }}><Icon name="x" size={14} /></button>
        </div>
      ))}
    </div>
  );
}

export default function Repos() {
  const { user } = useAuth();
  const { repos, meta, health, loadRepos, loadBoard } = useReview();
  const [busy, setBusy] = useState("");
  const [openId, setOpenId] = useState(null);
  const [notice, setNotice] = useState("");

  const refresh = async () => {
    setBusy("refresh");
    try {
      await api.refreshRepos();
      await loadRepos();
    } catch (e) {
      setNotice(e.message);
    } finally {
      setBusy("");
    }
  };

  const toggle = async (repo) => {
    setBusy(String(repo.id));
    setNotice("");
    try {
      const res = await api.enableRepo(repo.id, !repo.enabled);
      if (res.warning) setNotice(res.warning);
      await Promise.all([loadRepos(), loadBoard()]);
    } catch (e) {
      setNotice(e.message);
    } finally {
      setBusy("");
    }
  };

  const mode = meta.public_url ? "Webhooks (real-time)" : meta.poller > 0 ? `Polling every ${meta.poller}s` : "Manual sync only";
  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Repositories</h1><p className="muted">Enable a repository and Warren reviews its pull requests. Delivery mode: <b>{mode}</b>.</p></div>
        <button className="btn small" onClick={refresh} disabled={busy === "refresh" || user.demo}>{busy === "refresh" ? <Spinner /> : <Icon name="refresh" size={14} />} Import from GitHub</button>
      </div>
      {notice && <div className="alert">{notice}</div>}

      {health && (
        <div className="panel pad integrations">
          <span className={health.llm?.ok ? "ok" : "off"}><i />LLM gateway {health.llm?.ok ? `· ${health.llm.model}` : "· unreachable"}</span>
          <span className={health.sandbox ? "ok" : "off"}><i />E2B sandbox {health.sandbox ? "" : "· no API key (reviews run without code execution)"}</span>
          <span className="ok"><i />ChromaDB · {health.embeddings} embeddings</span>
          <span className={health.github_oauth ? "ok" : "off"}><i />GitHub OAuth</span>
        </div>
      )}

      <div className="panel">
        {repos.length === 0 && <div className="pad muted">No repositories yet — click “Import from GitHub”.</div>}
        {repos.map((r) => (
          <div key={r.id} className="repo">
            <div className="repo-row">
              <div className="grow">
                <b>{r.full_name}</b> {r.private && <Pill>private</Pill>} {r.enabled && <Pill tone="tag-ok">active</Pill>}
                <div className="muted small">{r.open_prs} open PR{r.open_prs === 1 ? "" : "s"} {r.last_synced ? `· synced ${timeAgo(r.last_synced)}` : ""} {r.webhook ? "· webhook installed" : ""}</div>
              </div>
              {r.enabled && <button className="btn small ghost" onClick={() => setOpenId(openId === r.id ? null : r.id)}>Settings</button>}
              <button className={`btn small ${r.enabled ? "" : "primary"}`} onClick={() => toggle(r)} disabled={busy === String(r.id) || user.demo}>
                {busy === String(r.id) ? <Spinner /> : null}{r.enabled ? "Disable" : "Enable"}
              </button>
            </div>
            {openId === r.id && <SettingsPanel repo={r} readOnly={user.demo} onSaved={loadRepos} />}
          </div>
        ))}
      </div>
    </div>
  );
}
