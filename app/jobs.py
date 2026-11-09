"""内存任务存储 + SSE 事件总线。

- Job 保存状态与事件历史;SSE 订阅者通过 asyncio.Queue 收到实时事件。
- 单 worker 部署(demo 规模)足够;多 worker 需换 Redis,README 有说明。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    prompt: str
    ratio_label: str
    ratio: float
    height: int
    status: str = "pending"  # pending | planning | generating | done | error
    n_tiles: int = 0
    done_tiles: int = 0
    beats: list = field(default_factory=list)
    result_url: str | None = None
    width: int = 0
    final_height: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    elapsed: float = 0.0
    events: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "prompt": self.prompt, "ratio_label": self.ratio_label,
            "ratio": self.ratio, "height": self.height, "status": self.status,
            "n_tiles": self.n_tiles, "done_tiles": self.done_tiles, "beats": self.beats,
            "result_url": self.result_url, "width": self.width, "height_out": self.final_height,
            "error": self.error, "created_at": self.created_at, "elapsed": self.elapsed,
        }


class JobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def create(self, prompt: str, ratio_label: str, ratio: float, height: int) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], prompt=prompt, ratio_label=ratio_label,
                  ratio=ratio, height=height)
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list_recent(self, limit: int = 30) -> list[dict]:
        jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]
        return [j.to_dict() for j in jobs]

    def publish(self, job: Job, event: dict) -> None:
        event["t"] = round(time.time(), 3)
        job.events.append(event)
        for q in list(job.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 慢消费者丢弃实时事件,可从 events 历史补齐

    def subscribe(self, job: Job) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        job.subscribers.append(q)
        return q

    def unsubscribe(self, job: Job, q: asyncio.Queue) -> None:
        if q in job.subscribers:
            job.subscribers.remove(q)


store = JobStore()
