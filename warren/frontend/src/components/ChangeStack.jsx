import { useMemo, useState } from "react";
import { PatchView } from "./DiffBlock";
import { Icon, Pill, SEVERITY_TONE } from "./ui";

const NAV = [["overview", "Overview", "home"], ["blast", "Security blast radius", "radio"], ["arch", "Architecture impact", "db"], ["verify", "Verify", "flask"]];

function FileDiff({ file }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="filediff">
      <button className="filehead" onClick={() => setOpen(!open)}>
        <span className="chev">{open ? "⌄" : "›"}</span><span className="lang">{file.path.split(".").pop().toUpperCase()}</span>
        <span className="mono">{file.path}</span>
        <span className="stat"><i className="add">+{file.additions}</i> <i className="del">−{file.deletions}</i></span>
      </button>
      {open && <PatchView patch={file.patch} />}
    </div>
  );
}

export default function ChangeStack({ stack, pr, repo }) {
  const [tab, setTab] = useState("layers");
  const [sel, setSel] = useState("overview");
  const layers = stack.layers || [];
  const files = useMemo(() => layers.flatMap((l) => l.files), [layers]);
  if (!layers.length) return <div className="timeline-card"><div className="muted pad">The Change Stack is generated during a review. Run a review to see layers, blast radius and architecture impact.</div></div>;

  const layer = sel.startsWith("layer-") && layers.find((l) => `layer-${l.id}` === sel);
  const file = sel.startsWith("file-") && files.find((f) => `file-${f.path}` === sel);
  const ov = stack.overview || {};

  return (
    <div className="stack">
      <div className="stack-head">
        <Pill tone="tag-ok"><Icon name="check" size={12} /> {pr.state === "open" ? "Open" : pr.state}</Pill>
        <b>{pr.title}</b><span className="muted">|</span>
        <span className="mono muted">{repo.full_name} #{pr.number} · {ov.files} files</span>
      </div>
      <div className="stack-body">
        <aside>
          <div className="seg">
            <button className={tab === "layers" ? "on" : ""} onClick={() => setTab("layers")}><Icon name="layers" size={14} /> Layers</button>
            <button className={tab === "files" ? "on" : ""} onClick={() => setTab("files")}><Icon name="file" size={14} /> Files</button>
          </div>
          {NAV.map(([key, label, icon]) => (
            <button key={key} className={`snav ${sel === key ? "on" : ""}`} onClick={() => setSel(key)}><Icon name={icon} size={15} /> {label}</button>
          ))}
          <hr />
          {tab === "layers"
            ? layers.map((l) => (
                <button key={l.id} className={`layer ${sel === `layer-${l.id}` ? "on" : ""}`} onClick={() => setSel(`layer-${l.id}`)}>
                  <span className="num">{l.id}</span>
                  <span><b>{l.title}</b><Pill>{l.files.length} file{l.files.length > 1 ? "s" : ""} touched</Pill></span>
                </button>))
            : files.map((f) => (
                <button key={f.path} className={`snav mono ${sel === `file-${f.path}` ? "on" : ""}`} onClick={() => setSel(`file-${f.path}`)}>{f.path}</button>))}
        </aside>

        <section className="stack-main">
          {sel === "overview" && (
            <div className="pad">
              <h3>What changed</h3><p>{ov.summary}</p>
              {ov.intent && <p className="muted">Intent: {ov.intent}</p>}
              <div className="kpis">
                <div><b>{ov.files}</b><span>files</span></div>
                <div><b className="add">+{ov.additions}</b><span>added</span></div>
                <div><b className="del">−{ov.deletions}</b><span>removed</span></div>
                <div><b>{ov.risk}</b><span>risk</span></div>
              </div>
              {Object.keys(stack.dependents || {}).length > 0 && (
                <><h4>Downstream dependents</h4>
                  {Object.entries(stack.dependents).map(([p, u]) => <div key={p} className="small"><span className="mono">{p}</span> <span className="muted">← {u.join(", ")}</span></div>)}</>
              )}
            </div>
          )}
          {sel === "blast" && (
            <div className="pad">
              <h3>Security blast radius</h3>
              {(stack.blast_radius || []).length === 0 && <p className="muted">No sensitive areas detected.</p>}
              {(stack.blast_radius || []).map((b, i) => (
                <div key={i} className="infocard"><div><b>{b.area}</b> <Pill tone={SEVERITY_TONE[b.severity]}>{b.severity}</Pill></div><p>{b.impact}</p>
                  <div className="mono small muted">{(b.files || []).join("  ·  ")}</div></div>
              ))}
            </div>
          )}
          {sel === "arch" && (
            <div className="pad">
              <h3>Architecture impact</h3>
              {(stack.architecture || []).map((a, i) => <div key={i} className="infocard"><b>{a.component}</b><p>{a.change}</p><p className="muted small">{a.note}</p></div>)}
            </div>
          )}
          {sel === "verify" && (
            <div className="pad">
              <h3>Verification</h3>
              {(stack.verify || []).map((v, i) => (
                <div key={i} className="check"><span className={`ck ${v.status}`}>{v.status === "passed" ? "✓" : v.status === "failed" ? "✕" : "–"}</span>
                  <div><b>{v.name}</b><div className="muted small">{v.detail}</div></div></div>
              ))}
            </div>
          )}
          {layer && (
            <div>
              <div className="pad"><h3>{layer.title}</h3><p className="muted">{layer.description}</p></div>
              {layer.files.map((f) => <FileDiff key={f.path} file={f} />)}
            </div>
          )}
          {file && <FileDiff file={file} />}
        </section>
      </div>
    </div>
  );
}
