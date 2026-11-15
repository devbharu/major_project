import { memo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { Avatar, Icon, Pill, githubAvatar } from "./ui";

const CI_PILL = { fixed: ["CI fix pushed", "tag-ok"], resolved: ["CI green", "tag-ok"], dismissed: ["CI dismissed", ""], awaiting_approval: ["CI fix needs approval", "tag-warn"],
  escalated: ["CI needs a human", "tag-warn"], proposed: ["CI fix suggested", "tag-run"], inherited: ["CI failing on main too", "tag-warn"] };

const tagTone = (tag) =>
  tag === "Needs human review" || tag === "High risk" || tag === "Needs author action" ? "tag-warn" : "";

function PRCard({ pr, onChange, readOnly }) {
  const navigate = useNavigate();
  const [menu, setMenu] = useState(false);
  const [busy, setBusy] = useState("");
  const people = [...new Set([...pr.requested_reviewers])];
  const open = (e) => e.target.closest(".assign") || navigate(`/pr/${pr.id}`);

  const assign = async (login) => {
    setBusy(login);
    try {
      await api.assign(pr.id, [login]);
      setMenu(false);
      onChange?.();
    } catch (e) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  };

  const sevs = pr.findings || {};
  const hot = (sevs.critical || 0) + (sevs.major || 0);
  return (
    <article className="prcard" onClick={open}>
      <div className="prmeta"><b>#{pr.number}</b><span className="dot">•</span>{pr.repo}</div>
      <h3>{pr.title}</h3>
      <p className="reason">{pr.reason}</p>
      <div className="tags">
        <Pill tone={`prio ${pr.priority.toLowerCase()}`}>{pr.priority}</Pill>
        {pr.tags.map((t) => <Pill key={t} tone={tagTone(t)}>{t}</Pill>)}
        {hot > 0 && <Pill tone="tag-warn">{hot} open finding{hot > 1 ? "s" : ""}</Pill>}
        {pr.review_status === "running" && <Pill tone="tag-run">Reviewing…</Pill>}
        {pr.ci && <Pill tone={CI_PILL[pr.ci]?.[1] ?? "tag-warn"}>{CI_PILL[pr.ci]?.[0] ?? "CI failing"}</Pill>}
      </div>
      <footer>
        <span className="author">@{pr.author}{pr.is_agent ? "" : ""}</span>
        <div className="assign">
          {people.length > 0 ? (
            <div className="avatars">{people.slice(0, 3).map((l) => <Avatar key={l} src={githubAvatar(l)} name={l} size={26} />)}</div>
          ) : (
            <>
              <button className="iconbtn" title="Assign a reviewer" disabled={readOnly} onClick={() => setMenu(!menu)}><Icon name="userplus" size={15} /></button>
              {menu && (
                <div className="popover">
                  <div className="muted small">Suggested reviewers</div>
                  {pr.reviewers.length === 0 && <div className="muted small">Run a review to get suggestions.</div>}
                  {pr.reviewers.map((l) => (
                    <button key={l} className="menuitem" onClick={() => assign(l)} disabled={busy === l}>
                      <Avatar src={githubAvatar(l)} name={l} size={20} /> {l}
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </footer>
    </article>
  );
}

export default memo(PRCard);
