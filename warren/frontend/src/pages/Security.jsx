import { useCallback, useEffect, useState } from "react";
import { api, waitForJob } from "../api/client";
import { Icon, Pill, SEVERITY_TONE, Spinner, timeAgo } from "../components/ui";
import { useAuth } from "../context/AuthContext";

const BUCKETS = [["critical", "Critical", "#f85149"], ["high", "High", "#f0883e"], ["medium", "Medium", "#d4a72c"], ["low", "Low", "#5b8def"], ["healthy", "Healthy", "#3fb950"], ["unscanned", "Unscanned", "#4a4a55"]];

function Distribution({ totals }) {
  const sum = BUCKETS.reduce((a, [k]) => a + (totals[k] || 0), 0) || 1;
  return (
    <div>
      <div className="distbar">{BUCKETS.map(([k, , color]) => totals[k] > 0 && <i key={k} style={{ width: `${(100 * totals[k]) / sum}%`, background: color }} title={`${k}: ${totals[k]}`} />)}</div>
      <div className="legend">{BUCKETS.map(([k, label, color]) => <span key={k}><i style={{ background: color }} />{label} <b>{totals[k] || 0}</b></span>)}</div>
    </div>
  );
}

function Trend({ trend }) {
  if (trend.length < 2) return <div className="muted small">Run scans over time to see the trend.</div>;
  const W = 520, H = 120, max = Math.max(1, ...trend.map((t) => t.critical + t.high + t.medium + t.low));
  const bw = W / trend.length;
  return (
    <svg viewBox={`0 0 ${W} ${H + 18}`} className="trend">
      {trend.map((t, i) => {
        let y = H;
        return (
          <g key={i}>
            {[["low", "#5b8def"], ["medium", "#d4a72c"], ["high", "#f0883e"], ["critical", "#f85149"]].map(([k, c]) => {
              const h = (t[k] / max) * (H - 6);
              y -= h;
              return <rect key={k} x={i * bw + 4} y={y} width={bw - 8} height={h} rx="2" fill={c} />;
            })}
            <text x={i * bw + bw / 2} y={H + 13} textAnchor="middle" fontSize="9" fill="#8b8b98">{t.date.slice(5)}</text>
          </g>
        );
      })}
    </svg>
  );
}

export default function Security() {
  const { user } = useAuth();
  const [data, setData] = useState(null);
  const [repoId, setRepoId] = useState("");
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => api.securityOverview(repoId).then(setData).catch((e) => setError(e.message)), [repoId]);
  useEffect(() => { load(); }, [load]);

  const scan = async () => {
    const target = repoId || data?.repos?.[0]?.id;
    if (!target) return setError("Connect a repository first.");
    setError("");
    try {
      const { job_id } = await api.securityScan(target);
      await waitForJob(job_id, setProgress);
    } catch (e) {
      setError(e.message);
    } finally {
      setProgress("");
      load();
    }
  };

  const openFixPr = async (f) => {
    try {
      const { url } = await api.securityFixPr(f.id);
      window.open(url, "_blank");
      load();
    } catch (e) {
      setError(e.message);
    }
  };

  if (!data) return <div className="center muted">{error || "Loading…"}</div>;
  const healthTone = { Healthy: "tag-ok", "Mostly healthy": "tag-ok", "Needs attention": "tag-warn", "At risk": "tag-warn" }[data.health] || "";

  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Security</h1><p className="muted">Dependency advisories (OSV) plus an AI deep scan of your riskiest files — verified before they reach you.</p></div>
        <div className="head-actions">
          {data.repos.length > 1 && <select value={repoId} onChange={(e) => setRepoId(e.target.value)}><option value="">All repositories</option>{data.repos.map((r) => <option key={r.id} value={r.id}>{r.full_name}</option>)}</select>}
          <button className="btn primary small" onClick={scan} disabled={!!progress || user.demo}>{progress ? <Spinner /> : <Icon name="radio" size={14} />} Run scan</button>
        </div>
      </div>
      {progress && <div className="banner"><Spinner /> {progress}</div>}
      {error && <div className="alert">{error}</div>}

      <div className="grid2">
        <div className="panel pad">
          <div className="eyebrow">Codebase health</div>
          <h2 className="bighealth">{data.health} <Pill tone={healthTone}>{data.scanned_repos} repo{data.scanned_repos === 1 ? "" : "s"} scanned</Pill></h2>
          <Distribution totals={data.totals} />
          {data.last_scan && <p className="muted small">Last scan {timeAgo(data.last_scan)}</p>}
        </div>
        <div className="panel pad"><div className="eyebrow">Findings over time</div><Trend trend={data.trend} /></div>
      </div>

      <div className="panel">
        <div className="pad eyebrow">Open findings ({data.findings.length})</div>
        {data.findings.length === 0 && <div className="pad muted">{data.scanned_repos ? "No open findings." : "No scan yet — click “Run scan”."}</div>}
        {data.findings.map((f) => (
          <div key={f.id} className="frow">
            <Pill tone={SEVERITY_TONE[f.severity]}>{f.severity}</Pill>
            <div className="grow">
              <b>{f.title}</b>
              <div className="muted small">{f.body}</div>
              <div className="mono small muted">{f.repo} · {f.path}{f.line ? `:${f.line}` : ""}</div>
            </div>
            {f.status === "fixed" ? <Pill tone="tag-ok">{f.meta?.pr_url ? <a href={f.meta.pr_url} target="_blank" rel="noreferrer">Fix PR opened</a> : "Fixed"}</Pill>
              : f.source === "dependency" && f.meta?.fixed_in
                ? <button className="btn small primary" disabled={user.demo} onClick={() => openFixPr(f)}>Fix ready · {f.meta.fixed_in}</button>
                : <Pill>{f.source === "security" ? "AI verified" : "no fix yet"}</Pill>}
          </div>
        ))}
      </div>
    </div>
  );
}
