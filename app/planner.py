"""分镜规划器:把用户的一条提示词规划成 N 个"从左到右"连贯的 tile 级提示词。

两种模式:
1. template(默认):纯模板,零额外依赖。给每个 tile 附加空间位置语义与一致性约束。
2. llm(可选):调用 chat 模型产出一段"从左到右的叙事分镜",每个 tile 有明确内容主题,
   叙事连贯性更好。失败时自动回退 template。
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from .config import settings

log = logging.getLogger("ultrawide.planner")

GLOBAL_STYLE = (
    "Seamless ultra-wide panoramic image. "
    "Keep a consistent style, color palette, lighting direction and horizon height across the whole picture. "
    "Photographic quality, no borders, no frames, no watermark, no text."
)

POSITION_HINTS_ZH = {
    "first": "这是整幅全景画面的最左侧部分,画面在右边缘之外仍会延续,构图不要在右侧收尾",
    "middle": "这是整幅全景画面的中段,与左右两侧的内容自然衔接",
    "last": "这是整幅全景画面的最右侧部分,画面在左边缘之外仍会延续,构图不要在左侧收尾",
}

CONTINUE_RULE = (
    "The LEFT strip of the input image is the tail of the previous section (already finished). "
    "Continue the scene seamlessly to the RIGHT: extend the same environment, "
    "keep the horizon line, perspective, lighting and color palette perfectly consistent. "
    "The continuation must read as ONE single uninterrupted photograph/picture. "
    "Do not repeat the left strip's content, do not add borders, frames, seams, text or watermarks."
)


def _template_beats(user_prompt: str, n: int) -> list[str]:
    beats = []
    for i in range(n):
        pos = "first" if i == 0 else ("last" if i == n - 1 else "middle")
        beats.append(f"{user_prompt} —— 第 {i + 1}/{n} 段({POSITION_HINTS_ZH[pos]})")
    return beats


async def _llm_beats(user_prompt: str, n: int) -> list[str] | None:
    """调用 chat 模型规划从左到右的分镜,返回每个 tile 的内容描述;失败返回 None。"""
    if not settings.planner_model or not settings.api_key:
        return None
    sys_prompt = (
        "你是超宽全景图分镜规划师。把用户的画面需求拆解成 N 个从左到右连续排布的场景段落,"
        "要求:N 段内容同属一个整体场景、空间上左右相邻、光照与色调统一、有清晰的视觉流向。"
        "只输出 JSON,格式:{\"beats\": [\"段落1描述\", ...]},每段 30~60 字,使用与用户相同的语言。"
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                json={
                    "model": settings.planner_model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": f"需求:{user_prompt}\nN={n}"},
                    ],
                    "temperature": 0.7,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", text, re.DOTALL)
            beats = json.loads(m.group(0))["beats"]
            if isinstance(beats, list) and len(beats) == n and all(isinstance(b, str) and b.strip() for b in beats):
                return [b.strip() for b in beats]
            log.warning("LLM 分镜数量不符(%s != %s),回退模板", len(beats), n)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM 分镜规划失败,回退模板: %s", e)
    return None


def first_tile_prompt(user_prompt: str, beat: str, n: int, aspect: float) -> str:
    return (
        f"{beat}\n\n{GLOBAL_STYLE}\n"
        f"This image is the LEFT part (section 1 of {n}) of an ultra-wide panorama "
        f"with an overall aspect ratio of about {aspect:.1f}:1. "
        "Compose it so the scene clearly continues beyond the right edge; "
        "do not close or frame the composition on the right side."
    )


def extend_tile_prompt(user_prompt: str, beat: str, i: int, n: int) -> str:
    return (
        f"{beat}\n\nOriginal request for the whole panorama: {user_prompt}\n"
        f"This is section {i + 1} of {n}. {CONTINUE_RULE}"
    )


async def plan(user_prompt: str, n_tiles: int, aspect: float) -> list[dict]:
    """返回每个 tile 的提示词方案:[{"beat", "prompt"}...]"""
    beats = await _llm_beats(user_prompt, n_tiles) or _template_beats(user_prompt, n_tiles)
    plans = []
    for i, beat in enumerate(beats):
        if i == 0:
            prompt = first_tile_prompt(user_prompt, beat, n_tiles, aspect)
        else:
            prompt = extend_tile_prompt(user_prompt, beat, i, n_tiles)
        plans.append({"beat": beat, "prompt": prompt})
    return plans
