import { useState } from "react";
import { api } from "../api/client";
import { DiffBlock } from "./DiffBlock";
import { Avatar, Icon, Logo, Markdown, Pill, SEVERITY_TONE, Spinner, timeAgo } from "./ui";

const VERB = { summary: "reviewed", finding: "flagged", fix: "replied", verify: "checked", reply: "replied", comment: "commented", learning: "learned", error: "reported", note: "noted" };
const STATUS_TONE = { open: "", fixed: "tag-ok", verified: "tag-ok", dismissed: "", failed: "tag-warn" };

function ActorIcon({ event }) {
  if (event.actor_kind === "bot") return <Logo size={36} />;
  if (event.actor_kind === "agent") return <span className="agent-badge"><Icon name="wrench" size={18} /></span>;
  return <Avatar src={`https://github.com/${event.actor}.png?size=72`} name={event.actor} size={36} square />;
}

export default function Timeline({ events, findings, pr, repo, readOnly, onAction, busyJob }) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const byId = Object.fromEntries(findings.map((f) => [f.id, f]));

  const act = async (fn) => {
    try {
      await onAction(fn);
    } catch (e) {
      alert(e.message);
    }
  };

  const send = async (e) => {
    e.preventDefault();
    if (!message.trim()) return;
    setSending(true);
    await act(() => api.chat(pr.id, message.trim()));
    setMessage("");
    setSending(false);
  };

  return (
    <div className="timeline-card">
      <div className="tl-head"><span className="mono">{repo.full_name} #{pr.number}</span><span className="muted">{events.length} update{events.length === 1 ? "" : "s"}</span></div>
      <div className="tl-body">
        {events.length === 0 && <div className="muted pad">No review yet. Click “Run review” to let Warren analyse this pull request.</div>}
        {events.map((e) => {
          const finding = e.meta?.finding_id && byId[e.meta.finding_id];
          return (
            <div key={e.id} className="tl-item">
              <ActorIcon event={e} />
              <div className={`bubble ${e.kind === "error" ? "bubble-error" : ""}`}>
                <div className="bubble-head">
                  <b>{e.actor}</b>
                  {e.actor_kind !== "human" && <Pill>{e.actor_kind === "agent" ? "coding agent" : "bot"}</Pill>}
                  <span className="muted">{VERB[e.kind] || "wrote"} {timeAgo(e.created_at)}</span>
                  {e.kind === "finding" && <Pill tone={SEVERITY_TONE[e.meta.severity]}>{e.meta.severity}</Pill>}
                  {finding && <Pill tone={STATUS_TONE[finding.status]}>{finding.status}</Pill>}
                </div>
                <div className="bubble-body">
                  {e.kind === "finding" && (
                    <div className="finding-title"><Icon name="warning" size={18} className="warn" /> <span>{e.body.split("\n")[0].replace(/\*\*/g, "")}</span></div>
                  )}
                  <Markdown text={e.kind === "finding" ? e.body.split("\n").slice(2).join("\n") : e.body} />
                  {e.meta?.path && <div className="mono small muted">{e.meta.path}{e.meta.line ? `:${e.meta.line}` : ""}</div>}
                  {e.meta?.new_code && <DiffBlock oldCode={e.meta.old_code} newCode={e.meta.new_code} />}
                  {e.kind === "finding" && finding?.status === "open" && (
                    <div className="actions">
                      <button className="btn small primary" disabled={readOnly || !!busyJob} onClick={() => act(() => api.fixFinding(finding.id))}><Icon name="wrench" size={13} /> Fix with Warren</button>
                      <button className="btn small ghost" disabled={readOnly} onClick={() => act(async () => { await api.dismissFinding(finding.id); return {}; })}>Dismiss</button>
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <form className="chatbox" onSubmit={send}>
        <input placeholder="Ask Warren about this PR, or teach it a team convention…" value={message} onChange={(e) => setMessage(e.target.value)} disabled={readOnly || sending} />
        <button className="btn primary small" disabled={readOnly || sending || !message.trim()}>{sending ? <Spinner /> : <Icon name="send" size={14} />} Send</button>
      </form>
    </div>
  );
}
