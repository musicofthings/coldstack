"""In-process async job runner for list builds.

A build is minutes long and the browser must not hold the connection open for it, so
the API starts a task and the client polls. This is deliberately the simplest thing
that works for P0; Celery takes over in P2 when sequences need to survive a restart.

The one thing it does properly is cancellation: a build that is spending the user's
money must be stoppable, and cancelling has to stop the spend, not just the polling.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"          # queued | running | done | failed | cancelled
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    result: Any = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "kind": self.kind, "status": self.status,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
        if self.status == "failed":
            out["error"] = self.error
        return out


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def start(self, kind: str, coro_factory: Callable[[], Awaitable[Any]]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        self._jobs[job.id] = job

        async def _run() -> None:
            job.status = "running"
            try:
                job.result = await coro_factory()
                job.status = "done"
            except asyncio.CancelledError:
                job.status = "cancelled"
                raise
            except Exception as exc:                       # noqa: BLE001
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = datetime.now(timezone.utc)

        job.task = asyncio.create_task(_run())
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or not job.task or job.task.done():
            return False
        job.task.cancel()
        return True

    def list_jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
