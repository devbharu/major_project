import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, waitForJob } from "../api/client";
import { PatchView } from "../components/DiffBlock";
import { Icon, Logo, Markdown, Pill, Spinner, timeAgo } from "../components/ui";
import { useAuth } from "../context/AuthContext";
import { useLive } from "../hooks/useLive";

const STEPS = [["detected", "Detected"], ["analyzing", "Analysing"], ["reproducing", "Reproducing"], ["fixing", "Fixing"], ["validating", "Validating"], ["awaiting_approval", "Approval"], ["fixed", "Fix pushed"], ["resolved", "Resolved"]];
const TONE = { fixed: "tag-ok", resolved: "tag-ok", proposed: "tag-run", dismissed: "", escalated: "tag-warn", failed: "tag-warn", awaiting_approval: "tag-warn", retrying: "tag-run" };
const LABEL = { resolved: "resolved", fixed: "fix pushed", proposed: "fix suggested on PR", awaiting_approval: "needs approval", escalated: "needs a human", retrying: "re-running" };
const stripHeader = (d) => d.split("\n").filter((l) => !l.startsWith("--- ") && !l.startsWith("+++ ")).join("\n");

function Stepper({ status }) {
  const idx = STEPS.findIndex(([k]) => k === status);
  const terminal = ["escalated", "failed", "dismissed", "retrying", "proposed"].includes(status);
  return (
    <div className="stepper">
      {STEPS.map(([k, label], i) => (
        <div key={k} className={`step ${!terminal && i < idx ? "done" : ""} ${k === status ? "now" : ""}`}><i />{label}</div>
      ))}
      {terminal && <div className="step now alt"><i />{LABEL[status] || status}</div>}
    </div>
  );
}

function IncidentList({ items, activeId }) {
  return (
    <div className="panel inc-list">
      {items.length === 0 && <div className="pad muted">No CI/CD failures detected. Warren watches every enabled repository.</div>}
      {items.map((i) => (
        <Link key={i.id} to={`/incidents/${i.id}`} className={`inc-row ${activeId === i.id ? "on" : ""}`}>
          <div className="grow">
            <b>{i.workflow}</b> <span className="muted">· {i.step_name || "run"}</span>
            <div className="muted small">{i.repo} · {i.pr_number ? `#${i.pr_number} ` : ""}{i.branch}</div>
            {i.meta?.also?.length > 0 && <div className="muted small">Also failing in {i.meta.also.join(", ")}</div>}
          </div>
          <div className="inc-meta">
            <Pill tone={TONE[i.status]}>{LABEL[i.status] || i.status}</Pill>
            <span className="muted small">{i.category?.replace("_", " ")} · {timeAgo(i.updated_at)}</span>
          </div>
        </Link>
      ))}
    </div>
  );
}

function Detail({ id, readOnly, onChanged }) {
  const [data, setData] = useState(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [showLog, setShowLog] = useState(false);

  const load = useCallback(() => api.incident(id).then(setData).catch((e) => setError(e.message)), [id]);
  useEffect(() => { setData(null); load(); }, [load]);
  useLive([`incident:${id}`], () => { load(); onChanged(); });

  const act = async (label, fn) => {
    setBusy(label);
    setError("");
    try {
      const res = await fn();
      if (res?.job_id) await waitForJob(res.job_id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
      load();
      onChanged();
    }
  };

  if (!data) return <div className="panel pad muted">{error || "Loading…"}</div>;
  const { incident: inc, events, job } = data;
  const diffs = Object.entries(inc.meta?.diffs || {});
  const working = ["detected", "analyzing", "reproducing", "fixing", "validating"].includes(inc.status) || job?.status === "running";

  return (
    <div className="inc-detail">
      <div className="panel pad">
        <div className="prmeta"><b>{inc.repo}</b><span className="dot">•</span>{inc.kind === "cd" ? "CD" : "CI"}<span className="dot">•</span>{inc.branch} @ {inc.head_sha?.slice(0, 7)}</div>
        <h2 className="inc-title">{inc.workflow} <span className="muted">›</span> {inc.job_name || "job"} <span className="muted">›</span> {inc.step_name || "step"}</h2>
        <div className="tags">
          <Pill tone={TONE[inc.status]}>{LABEL[inc.status] || inc.status}</Pill>
          {inc.category && <Pill>{inc.category.replace("_", " ")}</Pill>}
          {inc.confidence > 0 && <Pill>{Math.round(inc.confidence * 100)}% confidence</Pill>}
          {inc.meta?.flaky && <Pill tone="tag-warn">flaky (passed on re-run)</Pill>}
          {job?.seconds > 0 && <Pill>{job.seconds}s agent time</Pill>}
          {inc.pr_number && <Link to={`/pr/${inc.pr_id}`}><Pill>PR #{inc.pr_number}</Pill></Link>}
          <a href={inc.run_url} target="_blank" rel="noreferrer"><Pill><Icon name="external" size={11} /> failing run</Pill></a>
        </div>
        <Stepper status={inc.status} />
        {working && <div className="banner"><Spinner /> {job?.step || "Working…"}
          {job?.id && <button className="btn ghost small" onClick={() => api.cancelJob(job.id).then(load)}>Cancel</button>}</div>}
        {error && <div className="alert">{error}</div>}
      </div>

      {(inc.meta?.also?.length > 0 || inc.meta?.blocked_prs?.length > 0) && (
        <div className="panel pad">
          <div className="eyebrow">Also affected</div>
          {inc.meta.also?.length > 0 && <p>The same failure appears in {inc.meta.also.join(", ")}. One investigation covers all of them.</p>}
          {inc.meta.blocked_prs?.length > 0 && <p>Pull requests failing for this same reason: {inc.meta.blocked_prs.map((n) => `#${n}`).join(", ")}. They are not changed; re-run CI or rebase once this is fixed.</p>}
        </div>
      )}

      {(inc.root_cause || inc.solution) && (
        <div className="panel pad">
          {inc.root_cause && <><div className="eyebrow">Why it broke</div><Markdown text={inc.root_cause} /></>}
          {inc.solution && <><div className="eyebrow gap">How it is fixed</div><Markdown text={inc.solution} /></>}
          {inc.meta?.validation && <p className="small muted">Verification: {inc.meta.validation}</p>}
          {inc.fix_commit && <p className="small">Commit <code className="sha-inline">{inc.fix_commit.slice(0, 7)}</code></p>}
          {inc.fix_pr_url && <p className="small"><a className="linkbtn" href={inc.fix_pr_url} target="_blank" rel="noreferrer">Open the pull request →</a></p>}
          {inc.meta?.issue_url && <p className="small"><a className="linkbtn" href={inc.meta.issue_url} target="_blank" rel="noreferrer">Tracking issue →</a></p>}
        </div>
      )}

      {inc.status === "awaiting_approval" && (
        <div className="panel pad approval">
          <div className="eyebrow">Approval needed</div>
          <p>Warren paused before changing the repository because {(inc.meta?.approval_reasons || []).join("; ")}.</p>
          {diffs.map(([path, d]) => (
            <div key={path} className="filediff"><div className="filehead"><span className="mono">{path}</span></div><PatchView patch={stripHeader(d)} /></div>
          ))}
          <div className="actions gap">
            <button className="btn primary small" disabled={readOnly || !!busy} onClick={() => act("approve", () => api.approveIncident(id, true))}>{busy === "approve" ? <Spinner /> : <Icon name="check" size={14} />} Approve &amp; apply</button>
            <button className="btn small" disabled={readOnly || !!busy} onClick={() => act("reject", () => api.approveIncident(id, false))}>Reject</button>
          </div>
          <div className="steer">
            <input placeholder="…or steer it: “fix the test setup instead”, “bump the dependency, don’t edit code”" value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="btn small" disabled={readOnly || !note.trim() || !!busy} onClick={() => act("steer", () => api.approveIncident(id, false, note).then((r) => { setNote(""); return r; }))}>Steer</button>
          </div>
        </div>
      )}

      {["escalated", "failed", "proposed"].includes(inc.status) && diffs.length > 0 && (
        <div className="panel pad">
          <div className="eyebrow">Proposed change{inc.meta?.proposal_verified ? "" : " (not verified)"}</div>
          <p className="muted small">{inc.meta?.proposal_posted ? "Warren posted this on the pull request conversation. " : "Warren could not apply this itself. "}Review it and apply it, or add guidance below and re-run.</p>
          {diffs.map(([path, d]) => (
            <div key={path} className="filediff"><div className="filehead"><span className="mono">{path}</span></div><PatchView patch={stripHeader(d)} /></div>
          ))}
        </div>
      )}

      {["escalated", "failed", "proposed", "fixed", "resolved", "dismissed"].includes(inc.status) && (
        <div className="panel pad">
          <div className="eyebrow">Not right? Give Warren guidance and re-run</div>
          <div className="steer">
            <input placeholder="e.g. the failing test is outdated — update the fixture, don’t change the code" value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="btn small" disabled={readOnly || !!busy} onClick={() => act("retry", () => api.retryIncident(id, note).then((r) => { setNote(""); return r; }))}>{busy === "retry" ? <Spinner /> : <Icon name="refresh" size={14} />} Re-run analysis</button>
            {inc.status !== "dismissed" && <button className="btn ghost small" disabled={readOnly} onClick={() => act("dismiss", () => api.dismissIncident(id))}>Dismiss</button>}
          </div>
        </div>
      )}

      <div className="panel">
        <div className="pad eyebrow">Investigation log</div>
        <div className="inc-log">
          {events.map((e) => (
            <div key={e.id} className="inc-event">
              {e.actor_kind === "agent" ? <span className="agent-badge sm"><Icon name="wrench" size={13} /></span> : e.actor_kind === "bot" ? <Logo size={26} /> : <span className="agent-badge sm">you</span>}
              <div className="grow"><Markdown text={e.body} /><span className="muted small">{timeAgo(e.created_at)}</span></div>
            </div>
          ))}
        </div>
        {inc.log_excerpt && (
          <div className="pad">
            <button className="linkbtn" onClick={() => setShowLog(!showLog)}>{showLog ? "Hide" : "Show"} CI log excerpt</button>
            {showLog && <pre className="logpre">{inc.log_excerpt}</pre>}
          </div>
        )}
      </div>
    </div>
  );
}

export default function Incidents() {
  const { id } = useParams();
  const { user } = useAuth();
  const navigate = useNavigate();
  const [items, setItems] = useState([]);
  const load = useCallback(() => api.incidents().then((d) => setItems(d.incidents)).catch(() => {}), []);
  useEffect(() => { load(); }, [load]);
  useLive(["incidents"], load);
  useEffect(() => { if (!id && items.length) navigate(`/incidents/${items[0].id}`, { replace: true }); }, [id, items, navigate]);

  return (
    <div className="page wide">
      <div className="page-head">
        <div><h1>CI / CD incidents</h1><p className="muted">Warren watches your workflows. When one fails it finds why, reproduces it in a sandbox, fixes it, proves the fix, and asks before anything risky.</p></div>
      </div>
      <div className="inc-grid">
        <IncidentList items={items} activeId={Number(id)} />
        {id ? <Detail id={Number(id)} readOnly={user.demo} onChanged={load} /> : <div className="panel pad muted">Select an incident.</div>}
      </div>
    </div>
  );
}
