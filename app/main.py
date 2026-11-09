"""FastAPI 入口:静态前端 + 生成/进度/结果 API。

路由:
POST /api/generate              提交生成任务(异步执行,立即返回 job id)
GET  /api/jobs/{id}             任务状态(轮询降级方案)
GET  /api/jobs/{id}/events      SSE 实时进度流
GET  /api/jobs                  最近任务列表(历史记录)
GET  /api/results/{id}/final.png  最终全景图
GET  /api/results/{id}/tile/{n}.png 单块原图
GET  /api/capabilities          服务能力(tile 规格、可用比例、是否 mock)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import RESULTS_DIR, SAMPLES_DIR, settings
from .jobs import store
from .pipeline import compute_plan, run_job
from .provider import get_provider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ultrawide")

app = FastAPI(title="UltrawideForge", version="1.0.0")

_RATIO_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*[:/]\s*(\d{1,3}(?:\.\d+)?)\s*$")


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=2000)
    ratio: str = "6:1"                       # "6:1" / "8:1" / "12.5:1" / "21:9" …
    height: int = Field(default=1024, ge=256, le=2048)

    @field_validator("ratio")
    @classmethod
    def _check_ratio(cls, v: str) -> str:
        m = _RATIO_RE.match(v)
        if not m:
            raise ValueError("比例格式应为 W:H,例如 6:1、8:1、21:9")
        w, h = float(m.group(1)), float(m.group(2))
        if not 0.5 <= w / h <= 20:
            raise ValueError("比例需在 0.5:1 ~ 20:1 之间")
        return v

    def ratio_value(self) -> float:
        m = _RATIO_RE.match(self.ratio)
        w, h = float(m.group(1)), float(m.group(2))
        return w / h


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    ratio = req.ratio_value()
    provider = get_provider()
    job = store.create(req.prompt.strip(), req.ratio, ratio, req.height)
    asyncio.create_task(run_job(job))
    log.info("job %s accepted: ratio=%s h=%d provider=%s plan=%s", job.id, req.ratio, req.height,
             provider.name, compute_plan(ratio, req.height))
    return {"job_id": job.id, "plan": compute_plan(ratio, req.height), "provider": provider.name}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")

    async def stream():
        q = store.subscribe(job)
        try:
            for e in job.events:            # 先补发历史事件
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            if job.status in ("done", "error"):
                yield "event: end\ndata: {}\n\n"
                return
            while True:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # SSE 心跳,防止代理断连
                    continue
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                if e.get("type") in ("done", "error"):
                    yield "event: end\ndata: {}\n\n"
                    return
        finally:
            store.unsubscribe(job, q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": store.list_recent()}


def _safe_result_path(job_id: str, rel: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404, "not found")
    p = (RESULTS_DIR / job_id / rel).resolve()
    if not str(p).startswith(str(RESULTS_DIR.resolve())) or not p.exists():
        raise HTTPException(404, "not found")
    return p


@app.get("/api/results/{job_id}/final.png")
async def result_final(job_id: str):
    return FileResponse(_safe_result_path(job_id, "final.png"), media_type="image/png")


@app.get("/api/results/{job_id}/tile/{n}")
async def result_tile(job_id: str, n: int):
    return FileResponse(_safe_result_path(job_id, f"tiles/tile_{n}.png"), media_type="image/png")


@app.get("/api/samples")
async def samples():
    """samples/ 目录下的离线样例(6:1、8:1 实际案例),按 meta.json 列出。"""
    items = []
    if SAMPLES_DIR.exists():
        for meta in sorted(SAMPLES_DIR.glob("*/meta.json")):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                data["dir"] = meta.parent.name
                items.append(data)
            except Exception:  # noqa: BLE001
                continue
    return {"samples": items}


@app.get("/api/samples/{name}/final.png")
async def sample_final(name: str):
    if not re.fullmatch(r"[0-9a-zA-Z_\-]{1,64}", name):
        raise HTTPException(404, "not found")
    p = SAMPLES_DIR / name / "final.png"
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="image/png")


@app.get("/api/capabilities")
async def capabilities():
    provider = get_provider()
    return {
        "provider": provider.name,
        "model": getattr(provider, "model", None),
        "tile_width": settings.tile_width,
        "tile_height": settings.tile_height,
        "tile_ratio": round(settings.tile_ratio, 3),
        "max_supported_ratio": 20,
        "presets": ["3:1", "6:1", "8:1", "10:1", "12:1"],
        "mock": provider.name == "mock",
        "color_match": settings.color_match,
        "overlap_ratio": settings.overlap_ratio,
    }


static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
