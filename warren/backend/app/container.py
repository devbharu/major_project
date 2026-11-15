"""Dependency container: the shared objects every agent/route needs, created once in create_app()."""
import time
from dataclasses import dataclass, field

from . import crypto
from .config import settings
from .db import Database, now
from .jobs import JobRunner
from .services.bus import EventBus
from .services.github import GitHubClient
from .services.vectors import VectorStore

BOT_NAME = "warren"
AGENT_NAME = "warren-agent"


DEFAULT_REPO_SETTINGS = {
    "profile": "chill",            # quiet | chill | assertive  (how many findings are posted)
    "instructions": "",            # free-text guidelines the reviewer must follow
    "ignore_paths": ["*.lock", "package-lock.json", "yarn.lock", "dist/*", "build/*", "*.min.js", "*.svg", "*.png"],
    "auto_review": True,           # review every new PR / new commit automatically
    "auto_fix": False,             # apply fixes automatically after each review
    "post_comments": True,         # post the review to GitHub (off = dashboard only)
    "commit_status": True,         # publish a "warren/pre-merge" commit status that branch protection can require
    "ci_mode": "auto",             # CI Doctor: off = diagnose only | approval = always ask | auto = apply safe verified fixes
}


def repo_settings(repo: dict) -> dict:
    return {**DEFAULT_REPO_SETTINGS, **(repo.get("settings") or {})}


@dataclass
class Container:
    db: Database
    vectors: VectorStore
    jobs: JobRunner
    bus: EventBus
    checkpointer: object = None   # LangGraph SqliteSaver (CI Doctor runs are resumable)
    ci: object = None             # CiDoctor (set in create_app)
    _perm_cache: dict = field(default_factory=dict)

    def user_token(self, user_id: int) -> str:
        return crypto.decrypt(self.db.get("users", user_id)["token"])

    def user_gh(self, user_id: int) -> GitHubClient:
        return GitHubClient(self.user_token(user_id))

    def can_write(self, user_id: int, full_name: str, login: str) -> bool:
        """Does `login` have write access to the repo? Gate for bot commands and for what the bot is allowed to learn."""
        key = (full_name, login.lower())
        hit = self._perm_cache.get(key)
        if hit and time.time() - hit[1] < 600:
            return hit[0]
        try:
            allowed = self.user_gh(user_id).permission(full_name, login) in ("admin", "maintain", "write")
        except Exception:
            allowed = False
        self._perm_cache[key] = (allowed, time.time())
        return allowed

    def bot_gh(self, user_id: int) -> GitHubClient:
        """Client used for writing comments/commits: a dedicated bot token if configured, else the signed-in user's."""
        return GitHubClient(settings.github_bot_token) if settings.github_bot_token else self.user_gh(user_id)

    def repo_of(self, pr: dict) -> dict:
        return self.db.get("repos", pr["repo_id"])

    def emit(self, pr_id: int | None, actor: str, actor_kind: str, kind: str, body: str, meta: dict | None = None,
             incident_id: int | None = None) -> int:
        """Append an entry to a PR's / incident's timeline and push a live hint to connected dashboards."""
        eid = self.db.insert("events", {"pr_id": pr_id, "incident_id": incident_id, "actor": actor, "actor_kind": actor_kind,
                                        "kind": kind, "body": body, "meta": meta or {}, "created_at": now()})
        if pr_id:
            self.bus.publish(f"pr:{pr_id}", {"type": "event"})
        if incident_id:
            self.bus.publish(f"incident:{incident_id}", {"type": "event"})
        return eid

    def touch_board(self):
        self.bus.publish("board", {"type": "board"})
        self.bus.publish("incidents", {"type": "incidents"})
