import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, waitForJob } from "../api/client";
import ChangeStack from "../components/ChangeStack";
import Timeline from "../components/Timeline";
import { Icon, Pill, SEVERITY_TONE, Spinner } from "../components/ui";
import { useAuth } from "../context/AuthContext";
import { useLive } from "../hooks/useLive";

const CHECK_ICON = { passed: "✓", warning: "!", failed: "✕" };

export default function PRDetail() {
  const { id } = useParams();
  const { user } = useAuth();
  const [data, setData] = useState(null);
  const [tab, setTab] = useState("timeline");
  const [progress, setProgress] = useState("");
  const [jobId, setJobId] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await api.pr(id));
    } catch (e) {
      setError(e.message);
    }
  }, [id]);

  // run an action that returns { job_id }, show live progress, reload when finished
  const runJob = useCallback(async (start) => {
    setError("");
    try {
      const { job_id } = await start();
      setJobId(job_id);
      setProgress("Queued");
      await waitForJob(job_id, setProgress);
    } catch (e) {
      setError(e.message);
    } finally {
      setProgress("");
      setJobId(null);
      load();
    }
  }, [load]);

  useEffect(() => {
    load();
  }, [load]);
  useLive([`pr:${id}`], load);   // real-time: new findings, fixes and CI updates appear without refreshing

  // pick up a job that is already running (e.g. started by a webhook)
  useEffect(() => {
    if (data?.job && ["queued", "running"].includes(data.job.status) && !progress) runJob(async () => ({ job_id: data.job.id }));
  }, [data?.job?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!data) return <div className="center muted">{error || "Loading…"}</div>;
  const { pr, repo, events, findings, stack } = data;
  const open = findings.filter((f) => f.status === "open");
  const readOnly = user.demo;

  return (
    <div className="page detail">
      <div className="crumbs"><Link to="/">← Review board</Link></div>
      <div className="detail-head">
        <div>
          <div className="prmeta"><b>#{pr.number}</b><span className="dot">•</span>{repo.full_name}</div>
          <h1>{pr.title}</h1>
          <div className="tags">
            {(pr.triage?.tags || []).map((t) => <Pill key={t}>{t}</Pill>)}
            {pr.triage?.priority && <Pill tone={`prio ${pr.triage.priority.toLowerCase()}`}>{pr.triage.priority}</Pill>}
          </div>
          {pr.triage?.reason && <p className="muted">{pr.triage.reason}</p>}
        </div>
        <div className="head-actions">
          <a className="btn small" href={pr.url} target="_blank" rel="noreferrer"><Icon name="external" size={14} /> GitHub</a>
          <button className="btn small" disabled={readOnly || !!progress} onClick={() => runJob(() => api.review(pr.id))}><Icon name="play" size={13} /> Run review</button>
          <button className="btn small primary" disabled={readOnly || !!progress || open.length === 0} onClick={() => runJob(() => api.fixPr(pr.id))}>
            <Icon name="wrench" size={14} /> Autofix {open.length > 0 ? `(${open.length})` : ""}
          </button>
        </div>
      </div>

      {progress && <div className="banner"><Spinner /> <b>Working:</b> {progress}{jobId && <button className="btn ghost small" onClick={() => api.cancelJob(jobId)}>Cancel</button>}</div>}
      {(data.incidents || []).filter((i) => !["fixed", "dismissed"].includes(i.status)).map((i) => (
        <Link key={i.id} to={`/incidents/${i.id}`} className="banner ci-banner"><b>CI {i.status === "awaiting_approval" ? "fix needs your approval" : i.status === "escalated" ? "failure needs a human" : "is failing"}:</b> {i.workflow} › {i.step_name || "run"} — open the incident →</Link>
      ))}
      {error && <div className="alert">{error}</div>}
      {readOnly && <div className="banner muted">Demo mode is read-only — sign in with GitHub to run real reviews and autofixes.</div>}

      <div className="tabs">
        {[["timeline", "Review timeline"], ["stack", "Change Stack"], ["checks", "Pre-merge checks"]].map(([k, label]) => (
          <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{label}</button>
        ))}
        <span className="muted small tabs-right">{findings.length} finding{findings.length === 1 ? "" : "s"} · {open.length} open</span>
      </div>

      {tab === "timeline" && <Timeline events={events} findings={findings} pr={pr} repo={repo} readOnly={readOnly} busyJob={progress}
                                       onAction={(fn) => runJob(fn)} />}
      {tab === "stack" && <ChangeStack stack={stack} pr={pr} repo={repo} />}
      {tab === "checks" && (
        <div className="timeline-card">
          {(pr.premerge || []).length === 0 && <div className="muted pad">Checks appear after the first review.</div>}
          {(pr.premerge || []).map((c) => (
            <div key={c.name} className="check pad"><span className={`ck ${c.status}`}>{CHECK_ICON[c.status]}</span><div><b>{c.name}</b><div className="muted small">{c.explanation}</div></div></div>
          ))}
        </div>
      )}
    </div>
  );
}
