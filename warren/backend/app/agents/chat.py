"""Conversational agent: answers questions in PR threads, runs @mention commands, and learns team conventions."""
import logging
import re

from ..config import settings
from ..container import BOT_NAME, Container
from ..db import now
from ..services import llm
from ..services.github import GitHubError
from ..services.safety import clean_untrusted, safe_markdown, wrap_untrusted
from . import prompts
from .schemas import ChatOutput

log = logging.getLogger("warren.chat")


class ChatAgent:
    def __init__(self, container: Container):
        self.c = container

    def strip_mention(self, text: str) -> str:
        return re.sub(re.escape(settings.bot_mention), "", text, flags=re.I).strip()

    def respond(self, pr_id: int, text: str, author: str, comment_id: int | None = None,
                in_review_thread: bool = False, quote_question: bool = False, trusted: bool = True) -> str:
        from .autofix import AutofixAgent
        from .review import ReviewAgent

        db = self.c.db
        pr = db.get("pull_requests", pr_id)
        repo = db.get("repos", pr["repo_id"])
        question = clean_untrusted(self.strip_mention(text), 4000)
        self.c.emit(pr_id, author, "human", "comment", question)
        command = question.lower().split()[0] if question else ""

        is_command = command in ("review", "re-review", "rereview", "fix", "autofix", "fixes") or question.lower().startswith("full review")
        if is_command and not trusted:
            reply = "Only collaborators with write access to this repository can run Warren commands."
        elif command in ("review", "re-review", "rereview") or question.lower().startswith("full review"):
            self.c.jobs.submit("review", lambda p: ReviewAgent(self.c).run(pr_id, p, manual=True), pr_id=pr_id)
            reply = "Starting a fresh review of the latest commit. Results will appear in this conversation."
        elif command in ("fix", "autofix", "fixes"):
            self.c.jobs.submit("autofix", lambda p: AutofixAgent(self.c).run(pr_id, None, p), pr_id=pr_id)
            reply = "Preparing fixes for the open findings. Each fix is verified before it is committed to this branch, with a reply in the relevant thread."
        elif command in ("summary", "walkthrough"):
            counts = db.query("SELECT severity, COUNT(*) n FROM findings WHERE pr_id=? AND status='open' GROUP BY severity", (pr_id,))
            reply = f"**Summary**\n\n{pr['summary'] or 'No review has run yet. Comment `' + settings.bot_mention + ' review` to start one.'}\n\nOpen findings: " + (", ".join(f"{r['n']} {r['severity']}" for r in counts) or "none")
        else:
            reply = self._answer(pr, repo, question, author, comment_id, trusted)
        self.c.emit(pr_id, BOT_NAME, "bot", "reply", reply)
        self._post(repo, pr, reply if not quote_question else f"> {question}\n\n{reply}", comment_id, in_review_thread)
        return reply

    # ------------------------------------------------------------------
    def _answer(self, pr: dict, repo: dict, question: str, author: str, comment_id: int | None, trusted: bool = True) -> str:
        db = self.c.db
        findings = db.query("SELECT * FROM findings WHERE pr_id=? ORDER BY id DESC LIMIT 15", (pr["id"],))
        thread = next((f for f in findings if comment_id and f["comment_id"] == comment_id), None)
        stack = pr.get("change_stack") or {}
        patches = "\n".join(f"### {f['path']}\n{f['patch'][:1500]}" for l in stack.get("layers", []) for f in l["files"][:3])[:7000]
        context = (f"PR #{pr['number']}: {pr['title']}\n{(pr['body'] or '')[:800]}\nSummary: {pr['summary'] or 'n/a'}\n\n"
                   f"Findings:\n" + "\n".join(f"- [{f['status']}] {f['severity']} {f['title']} ({f['path']}:{f['line']})" for f in findings) +
                   (f"\n\nThe person is replying in the thread of this finding: {thread['title']}: {thread['body']}" if thread else "") +
                   f"\n\nDiff excerpts:\n{wrap_untrusted('diff', patches, 7000)}")
        try:
            out = llm.ask_json(prompts.CHAT_SYSTEM, f"{context}\n\nQuestion from {author}:\n{wrap_untrusted('question', question, 4000)}", ChatOutput, temperature=0.3)
        except Exception as exc:
            log.warning("chat LLM failed: %s", exc)
            return "I couldn't reach the language model just now. Please try again in a moment."
        reply = safe_markdown(out.reply, settings.bot_mention)
        if out.learning and trusted:
            lid = db.insert("learnings", {"repo_id": repo["id"], "text": clean_untrusted(out.learning, 600), "path_hint": out.learning_path or "",
                                          "source_pr": pr["number"], "created_by": author, "created_at": now()})
            self.c.vectors.add_learning(repo["id"], lid, clean_untrusted(out.learning, 600), out.learning_path or "")
            learning = safe_markdown(out.learning)
            reply += (f"\n\n<details><summary>Learning recorded</summary>\n\nRecorded for {repo['full_name']} from #{pr['number']} (requested by {author}):\n\n"
                      f"{learning}\n\n</details>")
            self.c.emit(pr["id"], BOT_NAME, "bot", "learning", learning, {"learning_id": lid})
        return reply

    def _post(self, repo, pr, text, comment_id, in_review_thread):
        bot = self.c.bot_gh(repo["user_id"])
        text = f"{text}\n\n<!-- warren:reply -->"
        try:
            if in_review_thread and comment_id:
                bot.reply_to_review_comment(repo["full_name"], pr["number"], comment_id, text)
            else:
                bot.issue_comment(repo["full_name"], pr["number"], text)
        except GitHubError:
            log.warning("could not post chat reply to GitHub", exc_info=True)
