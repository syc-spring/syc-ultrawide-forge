"""滚动外绘(rolling outpainting)管线:核心算法编排。

流程:
1. 解析目标比例与高度 → 在 tile 原生高度下计算目标总宽 target_w、tile 数 n。
2. 分镜规划(planner):生成 n 个从左到右连贯的 tile 提示词。
3. Tile 1:text-to-image 直接生成。
4. Tile k:build_edit_canvas(左端锚定条=累积图右缘 overlap_px,其余透明)→
   images/edits 续画 → 色彩匹配 → 重叠区线性羽化拼接。最后一块先按需裁宽,保证总宽精确。
5. 可选全局调和 pass(默认关闭)。
6. 输出按用户请求高度等比缩放(只降采样,不拉伸),落盘 results/{job_id}/。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import time

from PIL import Image

from . import planner, stitcher
from .config import RESULTS_DIR, settings
from .jobs import Job, store
from .provider import get_provider

log = logging.getLogger("ultrawide.pipeline")


def _thumb_b64(img: Image.Image, height: int = 200) -> str:
    w = round(img.width * height / img.height)
    buf = io.BytesIO()
    img.resize((w, height), Image.LANCZOS).save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def compute_plan(target_ratio: float, out_h: int) -> dict:
    """根据目标比例与输出高度计算 tile 几何。工作高度 = tile 原生高度,最后统一缩放。"""
    tile_w, tile_h = settings.tile_width, settings.tile_height
    overlap_px = max(64, round(tile_w * settings.overlap_ratio))
    work_h = tile_h
    target_w = round(work_h * target_ratio)

    if target_w <= tile_w:
        n = 1
    else:
        advance = tile_w - overlap_px
        n = 1 + math.ceil((target_w - tile_w) / advance)
    return {
        "tile_w": tile_w, "tile_h": tile_h, "overlap_px": overlap_px,
        "work_h": work_h, "target_w": target_w, "n_tiles": n,
        "out_w": round(out_h * target_ratio), "out_h": out_h,
    }


async def run_job(job: Job) -> None:
    t0 = time.time()
    provider = get_provider()
    geo = compute_plan(job.ratio, job.height)
    size = f"{geo['tile_w']}x{geo['tile_h']}"
    job.n_tiles = geo["n_tiles"]

    out_dir = RESULTS_DIR / job.id
    (out_dir / "tiles").mkdir(parents=True, exist_ok=True)

    def ev(**kw):
        store.publish(job, kw)

    try:
        # ---------- 1. 分镜规划 ----------
        job.status = "planning"
        ev(type="status", status="planning", message="正在规划分镜…")
        plans = await planner.plan(job.prompt, geo["n_tiles"], job.ratio)
        job.beats = [p["beat"] for p in plans]
        ev(type="planned", n_tiles=geo["n_tiles"], beats=job.beats, geo={k: geo[k] for k in ("tile_w", "tile_h", "overlap_px", "target_w", "out_w", "out_h")})

        # ---------- 2. Tile 1:文生图 ----------
        job.status = "generating"
        ev(type="tile_start", i=0, stage="generate", message=f"Tile 1/{geo['n_tiles']}(文生图)")
        img = await asyncio.to_thread(provider.generate, plans[0]["prompt"], size)
        accumulated = stitcher.resize_to(img, geo["tile_w"], geo["tile_h"])
        accumulated.save(out_dir / "tiles" / "tile_1.png")
        job.done_tiles = 1
        ev(type="tile_done", i=0, thumb=_thumb_b64(accumulated), done=1, total=geo["n_tiles"], width=accumulated.width)

        # ---------- 3. Tile 2..N:编辑续画 ----------
        for k in range(1, geo["n_tiles"]):
            ev(type="tile_start", i=k, stage="edit", message=f"Tile {k + 1}/{geo['n_tiles']}(续画)")
            canvas = stitcher.build_edit_canvas(accumulated, geo["tile_w"], geo["overlap_px"], fill=settings.edit_fill)
            edited = await asyncio.to_thread(provider.edit, plans[k]["prompt"], canvas.convert("RGB"), size)
            new_tile = stitcher.resize_to(edited, geo["tile_w"], geo["tile_h"])
            new_tile = stitcher.match_color(new_tile, accumulated, geo["overlap_px"])

            # 最后一块按剩余宽度裁齐(裁掉右侧多余,不拉伸)
            remaining = geo["target_w"] - (accumulated.width - geo["overlap_px"])
            if k == geo["n_tiles"] - 1 and 0 < remaining < new_tile.width:
                new_tile = new_tile.crop((0, 0, remaining, new_tile.height))

            accumulated = stitcher.stitch(accumulated, new_tile, geo["overlap_px"])
            accumulated = stitcher.crop_final(accumulated, geo["target_w"])
            accumulated.save(out_dir / "tiles" / f"tile_{k + 1}.png")
            job.done_tiles = k + 1
            ev(type="tile_done", i=k, thumb=_thumb_b64(new_tile), done=k + 1, total=geo["n_tiles"], width=accumulated.width)

        # ---------- 4. 输出 ----------
        # 单 tile 场景(目标宽 ≤ tile 宽)做居中裁切;多 tile 场景在循环内已裁齐,永不拉伸
        if geo["n_tiles"] == 1 and accumulated.width > geo["target_w"]:
            off = (accumulated.width - geo["target_w"]) // 2
            accumulated = accumulated.crop((off, 0, off + geo["target_w"], accumulated.height))
        final = stitcher.resize_to(accumulated, geo["out_w"], geo["out_h"])
        final_path = out_dir / "final.png"
        final.save(final_path, optimize=True)

        job.status = "done"
        job.width, job.final_height = final.width, final.height
        job.elapsed = round(time.time() - t0, 1)
        job.result_url = f"/api/results/{job.id}/final.png"
        (out_dir / "meta.json").write_text(
            json.dumps({
                **job.to_dict(), "provider": provider.name, "model": getattr(provider, "model", "mock"),
                "geo": {kk: vv for kk, vv in geo.items()}, "settings": {
                    "overlap_ratio": settings.overlap_ratio, "color_match": settings.color_match,
                },
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        ev(type="done", result_url=job.result_url, width=final.width, height=final.height,
           elapsed=job.elapsed, thumb=_thumb_b64(final, 120))
        log.info("job %s done: %dx%d in %.1fs", job.id, final.width, final.height, job.elapsed)

    except Exception as e:  # noqa: BLE001
        job.status = "error"
        job.error = str(e)[:800]
        job.elapsed = round(time.time() - t0, 1)
        ev(type="error", message=job.error)
        log.exception("job %s failed", job.id)
