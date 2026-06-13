"""In-process job manager for long-running bulk operations.

Bulk applies (and any other generator-driven SSE work) get wrapped in a
``Job`` so they keep running on the server thread even if the client's
network connection drops. The browser can re-subscribe to the same job
from where it left off via ``GET /jobs/<id>/stream?since=<offset>``;
events are buffered on the Job, so a 30-second WiFi outage doesn't lose
any rows.

Single-process design fits the app: one user, one frozen exe, jobs die
when the exe closes. State is in-memory only - we make no attempt to
persist jobs across restarts (that would require a real queue).
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Iterable, Iterator


class Job:
    """One in-flight bulk operation.

    Events are appended to ``self.events`` by the worker thread; HTTP
    request threads read them under ``self._lock``. The Condition is
    notified every append so subscribers blocked in
    ``iter_events`` wake immediately.
    """

    __slots__ = (
        "id", "kind", "label", "started_from", "started_at", "finished_at",
        "state", "events", "cancel_requested", "_lock", "_cv",
        "_total", "_last_progress",
    )

    def __init__(self, kind: str, label: str, started_from: str) -> None:
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.label = label
        self.started_from = started_from
        self.started_at = time.time()
        self.finished_at: float | None = None
        # "running" -> "done" | "error" | "cancelled"
        self.state = "running"
        self.events: list[dict] = []
        self.cancel_requested = False
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._total = 0
        self._last_progress: dict | None = None

    # ---------- writer (worker thread) ----------

    def append(self, ev: dict) -> None:
        with self._cv:
            self.events.append(ev)
            etype = ev.get("type")
            if etype == "start":
                self._total = int(ev.get("total") or 0)
            elif etype == "progress":
                self._last_progress = ev
            self._cv.notify_all()

    def finish(self, state: str) -> None:
        with self._cv:
            self.state = state
            self.finished_at = time.time()
            self._cv.notify_all()

    def request_cancel(self) -> None:
        with self._cv:
            self.cancel_requested = True
            self._cv.notify_all()

    # ---------- reader (HTTP threads) ----------

    def iter_events(self, since: int = 0, idle_timeout: float = 12.0) -> Iterator[tuple[int, dict | None]]:
        """Yield (offset, event) pairs from ``since`` onwards.

        Blocks until new events arrive or the job ends. When idle for
        ``idle_timeout`` seconds, yields ``(offset, None)`` so the caller
        can emit an SSE heartbeat (some WebView2 / WSGI proxies kill
        otherwise-silent connections after ~30s).
        """
        cur = since
        while True:
            with self._cv:
                # Drain anything new
                new_events = self.events[cur:]
                state = self.state
                if not new_events and state == "running":
                    # Block for next append OR end-of-job notify
                    self._cv.wait(idle_timeout)
                    new_events = self.events[cur:]
                    state = self.state
            if new_events:
                for ev in new_events:
                    yield cur, ev
                    cur += 1
                continue
            if state != "running":
                return
            # Idle timeout fired with no new work - heartbeat marker.
            yield cur, None

    def snapshot(self) -> dict:
        with self._lock:
            total = self._total
            lp = self._last_progress
            current = int(lp.get("current") or 0) if lp else 0
            return {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "started_from": self.started_from,
                "state": self.state,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "events_total": len(self.events),
                "current": current,
                "total": total,
                "percent": int(100 * current / total) if total else 0,
            }


# ---------- registry ----------

_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()
_RETENTION_SECONDS = 30 * 60  # finished jobs stay around for 30 min


def _prune_unlocked() -> None:
    now = time.time()
    drop = [
        jid
        for jid, j in _JOBS.items()
        if j.state != "running" and j.finished_at and (now - j.finished_at) > _RETENTION_SECONDS
    ]
    for jid in drop:
        _JOBS.pop(jid, None)


def start(
    kind: str,
    label: str,
    started_from: str,
    generator_factory: Callable[[Job], Iterable[dict]],
) -> Job:
    """Register a Job and spawn its worker thread.

    ``generator_factory`` receives the Job (so it can check
    ``job.cancel_requested`` between rows) and returns the event
    iterator.
    """
    with _JOBS_LOCK:
        _prune_unlocked()
    job = Job(kind=kind, label=label, started_from=started_from)
    with _JOBS_LOCK:
        _JOBS[job.id] = job

    def worker() -> None:
        try:
            for ev in generator_factory(job):
                job.append(ev)
                if job.cancel_requested:
                    job.append({"type": "cancelled", "message": "Cancelled by user."})
                    job.finish("cancelled")
                    return
                if ev.get("type") in ("error",):
                    job.finish("error")
                    return
        except Exception as exc:  # noqa: BLE001 - convert to an error event
            job.append({"type": "error", "message": str(exc)})
            job.finish("error")
            return
        # Factory generators may swallow their own loop on
        # job.cancel_requested and return early without yielding the
        # final event; honour the cancel at the boundary so the panel
        # gets a cancelled state instead of a misleading "done".
        if job.cancel_requested:
            job.append({"type": "cancelled", "message": "Cancelled by user."})
            job.finish("cancelled")
        else:
            job.finish("done")

    threading.Thread(
        target=worker,
        name=f"job-{job.id[:8]}",
        daemon=True,
    ).start()
    return job


def get(job_id: str) -> Job | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def list_active() -> list[dict]:
    with _JOBS_LOCK:
        _prune_unlocked()
        return [j.snapshot() for j in _JOBS.values() if j.state == "running"]


def list_recent(max_items: int = 10) -> list[dict]:
    with _JOBS_LOCK:
        _prune_unlocked()
        items = sorted(_JOBS.values(), key=lambda j: j.started_at, reverse=True)
        return [j.snapshot() for j in items[:max_items]]
