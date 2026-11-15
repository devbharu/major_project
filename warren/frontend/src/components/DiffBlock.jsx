// Two diff renderers: DiffBlock (before/after snippet inside a review comment) and PatchView (a real unified patch).
export function DiffBlock({ oldCode, newCode }) {
  if (!newCode) return null;
  const rows = [...(oldCode ? oldCode.split("\n").map((t) => ["del", t]) : []), ...newCode.split("\n").map((t) => ["add", t])];
  return (
    <pre className="diffblock">
      {rows.map(([kind, text], i) => (
        <div key={i} className={`dl ${kind}`}><span className="sign">{kind === "del" ? "−" : "+"}</span>{text || " "}</div>
      ))}
    </pre>
  );
}

export function PatchView({ patch }) {
  if (!patch) return <div className="muted small pad">No textual diff (binary or too large).</div>;
  let oldNo = 0, newNo = 0;
  const rows = patch.split("\n").map((raw, i) => {
    const hunk = raw.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
    if (hunk) {
      oldNo = +hunk[1]; newNo = +hunk[2];
      return <div key={i} className="pl hunk"><span className="no" /><span className="no" /><span className="tx">{raw}</span></div>;
    }
    const kind = raw[0] === "+" ? "add" : raw[0] === "-" ? "del" : "ctx";
    const row = (
      <div key={i} className={`pl ${kind}`}>
        <span className="no">{kind !== "add" ? oldNo : ""}</span>
        <span className="no">{kind !== "del" ? newNo : ""}</span>
        <span className="tx">{raw || " "}</span>
      </div>
    );
    if (kind !== "add") oldNo++;
    if (kind !== "del") newNo++;
    return row;
  });
  return <div className="patchview">{rows}</div>;
}
