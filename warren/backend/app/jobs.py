"""Background job runner. Webhooks answer immediately and enqueue work here - the same decoupling
CodeRabbit gets from Cloud Tasks + a long-running execution service. Jobs are cancellable and stream progress."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .db import Database, now
from .services.bus import EventBus

log = logging.getLogger("warren.jobs")


class JobCancelled(Exception):
    pass


class Progress:
    def __init__(self, db: Database, job_id: int, bus: EventBus, cancelled: set, topics: list[str]):
        self.db, self.job_id, self.bus, self._cancelled, self.topics = db, job_id, bus, cancelled, topics

    def step(self, text: str):
        if self.job_id in self._cancelled:   # cooperative cancellation at the next step boundary
            raise JobCancelled()
        log.info("job %s: %s", self.job_id, text)
        self.db.update("jobs", self.job_id, {"step": text, "updated_at": now()})
        for t in self.topics:
            self.bus.publish(t, {"type": "job", "job_id": self.job_id, "step": text})


class JobRunner:
    def __init__(self, db: Database, workers: int = 4, bus: EventBus | None = None):
        self.db, self.bus = db, bus or EventBus()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="warren-job")
        self._lock = threading.Lock()
        self._cancelled: set[int] = set()

    def submit(self, kind: str, fn, repo_id: int | None = None, pr_id: int | None = None, incident_id: int | None = None) -> int:
        """Queue `fn(progress)`. If the same kind of job is already queued/running for the same target, reuse it."""
        target = incident_id or 0
        with self._lock:
            active = self.db.one(
                "SELECT id FROM jobs WHERE kind=? AND COALESCE(pr_id,0)=? AND COALESCE(repo_id,0)=? AND COALESCE(incident_id,0)=? "
                "AND status IN ('queued','running')", (kind, pr_id or 0, repo_id or 0, target))
            if active:
                return active["id"]
            job_id = self.db.insert("jobs", {"kind": kind, "repo_id": repo_id, "pr_id": pr_id, "incident_id": incident_id, "status": "queued",
                                             "step": "Queued", "created_at": now(), "updated_at": now()})
        self.pool.submit(self._run, job_id, fn, self._topics(pr_id, incident_id))
        return job_id

    @staticmethod
    def _topics(pr_id, incident_id) -> list[str]:
        return [t for t in (f"pr:{pr_id}" if pr_id else None, f"incident:{incident_id}" if incident_id else None) if t]

    def cancel(self, job_id: int) -> bool:
        job = self.db.get("jobs", job_id)
        if not job or job["status"] not in ("queued", "running"):
            return False
        self._cancelled.add(job_id)
        return True

    def _run(self, job_id: int, fn, topics: list[str]):
        started = time.time()
        if job_id in self._cancelled:
            return self._finish(job_id, "cancelled", topics, started, "Cancelled")
        self.db.update("jobs", job_id, {"status": "running", "updated_at": now()})
        try:
            fn(Progress(self.db, job_id, self.bus, self._cancelled, topics))
            self._finish(job_id, "done", topics, started, "Done")
        except JobCancelled:
            self._finish(job_id, "cancelled", topics, started, "Cancelled")
        except Exception as exc:  # job failures must never crash the worker thread
            log.exception("job %s failed", job_id)
            self._finish(job_id, "failed", topics, started, "Failed", str(exc)[:500])

    def _finish(self, job_id, status, topics, started, step, error=None):
        self._cancelled.discard(job_id)
        self.db.update("jobs", job_id, {"status": status, "step": step, "error": error, "updated_at": now(),
                                        "seconds": int(time.time() - started)})
        for t in [*topics, "board"]:
            self.bus.publish(t, {"type": "job", "job_id": job_id, "status": status})
