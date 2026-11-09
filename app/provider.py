"""图像生成 Provider:封装 OpenAI 兼容的 images/generations 与 images/edits 接口。

设计要点:
- 同时兼容 gpt-image-1 系(默认返回 b64、不接受 response_format)与 dall-e 系(默认返回 url)。
- 统一重试 + 指数退避,图像生成为长耗时操作,read timeout 放宽到 300s。
- 返回 PIL.Image,尺寸若与请求不一致由调用方统一缩放。
"""
from __future__ import annotations

import base64
import io
import logging
import random
import time
from typing import Optional

import httpx
from PIL import Image

from .config import settings

log = logging.getLogger("ultrawide.provider")

MAX_RETRIES = 3
HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)


class ImageAPIError(RuntimeError):
    pass


def _decode_image(payload: dict) -> Image.Image:
    """从 API 响应中解出图片,兼容 b64_json / url 两种返回。"""
    data = payload["data"][0]
    if data.get("b64_json"):
        raw = base64.b64decode(data["b64_json"])
        return Image.open(io.BytesIO(raw)).convert("RGB")
    if data.get("url"):
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.get(data["url"])
            resp.raise_for_status()
            return Image.open(io.BytesIO(resp.content)).convert("RGB")
    raise ImageAPIError(f"响应中既无 b64_json 也无 url: {list(data.keys())}")


class OpenAIImageProvider:
    """OpenAI 兼容图像 API Provider(适用于 gpt-image-1 及各类兼容中转)。"""

    name = "openai"

    def __init__(self, api_key: str = "", base_url: str = "", model: str = ""):
        self.api_key = api_key or settings.api_key
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self.model = model or settings.image_model

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def generate(self, prompt: str, size: str, **kw) -> Image.Image:
        body: dict = {"model": self.model, "prompt": prompt, "n": 1, "size": size}
        # gpt-image-1 不接受 response_format 且默认 b64;dall-e 系需要显式指定
        if self.model.startswith("dall-e"):
            body["response_format"] = "b64_json"
        if not self.model.startswith("dall-e"):
            quality = kw.get("quality") or settings.image_quality
            if quality and quality != "default":
                body["quality"] = quality
        return self._request_json("/images/generations", json=body)

    def edit(self, prompt: str, image: Image.Image, size: str, **kw) -> Image.Image:
        """基于输入图像进行编辑/外绘。输入画布为 RGB。"""
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        files = {"image": ("canvas.png", buf.getvalue(), "image/png")}
        data: dict = {"model": self.model, "prompt": prompt, "n": 1, "size": size}
        if not self.model.startswith("dall-e"):
            quality = kw.get("quality") or settings.image_quality
            if quality and quality != "default":
                data["quality"] = quality
        return self._request_multipart("/images/edits", data=data, files=files)

    # ---- 底层请求 ----
    def _request_json(self, path: str, json: dict) -> Image.Image:
        return self._with_retry(lambda: self._post_json_once(path, json))

    def _request_multipart(self, path: str, data: dict, files: dict) -> Image.Image:
        return self._with_retry(lambda: self._post_multipart_once(path, data, files))

    def _with_retry(self, fn):
        last_err: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn()
            except (httpx.HTTPError, ImageAPIError) as e:
                last_err = e
                wait = min(2**attempt + random.random(), 20)
                log.warning("API 调用失败(第 %d/%d 次): %s,%.1fs 后重试", attempt, MAX_RETRIES, e, wait)
                if attempt < MAX_RETRIES:
                    time.sleep(wait)
        raise ImageAPIError(f"图像 API 连续 {MAX_RETRIES} 次调用失败: {last_err}")

    def _post_json_once(self, path: str, body: dict) -> Image.Image:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(f"{self.base_url}{path}", headers=self._headers, json=body)
        return self._parse(resp)

    def _post_multipart_once(self, path: str, data: dict, files: dict) -> Image.Image:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(f"{self.base_url}{path}", headers=self._headers, data=data, files=files)
        return self._parse(resp)

    @staticmethod
    def _parse(resp: httpx.Response) -> Image.Image:
        if resp.status_code != 200:
            raise ImageAPIError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            raise ImageAPIError(f"响应不是 JSON: {resp.text[:300]}") from e
        if "data" not in payload:
            raise ImageAPIError(f"响应缺少 data 字段: {str(payload)[:300]}")
        return _decode_image(payload)


class MockImageProvider:
    """确定性程序化图像 Provider,用于无 Key 环境下端到端验证整条管线。

    生成内容为与 prompt 相关的确定性渐变 + 几何纹理;
    edit() 会延续输入画布左缘的颜色趋势向右演化,模拟"续画"。
    """

    name = "mock"

    def _hash(self, s: str) -> int:
        h = 2166136261
        for ch in s:
            h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
        return h

    def _palette(self, seed: int) -> tuple:
        r = lambda k: ((seed >> k) & 0xFF) / 255.0  # noqa: E731
        return (int(40 + 150 * r(3)), int(60 + 140 * r(7)), int(80 + 160 * r(11)))

    def _render(self, w: int, h: int, seed: int, base_rgb=None) -> Image.Image:
        import numpy as np

        rng = np.random.default_rng(seed)
        c0 = base_rgb or self._palette(seed)
        c1 = tuple(min(255, int(c * (0.55 + 0.35 * ((seed >> 13) & 3) / 3))) for c in c0)
        x = np.linspace(0, 1, w)[None, :]
        y = np.linspace(0, 1, h)[:, None]
        img = np.zeros((h, w, 3), dtype=np.float32)
        for i in range(3):
            img[..., i] = c0[i] * (1 - x) + c1[i] * x  # 水平渐变
            img[..., i] += 25 * np.sin(6 * np.pi * y[:, 0])[:, None] * x  # 纹理波动
        # 叠加确定性几何"主体"
        for k in range(6):
            cx, cy = rng.random(), rng.random()
            rad = (0.04 + 0.10 * rng.random()) * h
            xx, yy = np.meshgrid(np.arange(w), np.arange(h))
            mask = ((xx - cx * w) ** 2 + (yy - cy * h) ** 2) < rad**2
            shade = 0.6 + 0.4 * ((seed >> (k * 3)) & 7) / 7
            img[mask] = img[mask] * shade + (255 - img[mask]) * (1 - shade) * 0.5
        return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))

    def generate(self, prompt: str, size: str, **kw) -> Image.Image:
        w, h = (int(v) for v in size.split("x"))
        return self._render(w, h, self._hash(prompt))

    def edit(self, prompt: str, image: Image.Image, size: str, **kw) -> Image.Image:
        w, h = (int(v) for v in size.split("x"))
        image = image.resize((w, h))
        # 取左缘 5% 列的均值作为续画起点色,保证色彩延续
        import numpy as np

        arr = np.asarray(image).astype(np.float32)
        edge = arr[:, : max(1, w // 20), :].mean(axis=(0, 1))
        seed = (self._hash(prompt) + int(edge.sum())) & 0x7FFFFFFF
        return self._render(w, h, seed, base_rgb=tuple(int(c) for c in edge))


def get_provider():
    if settings.mock or not settings.api_key:
        if not settings.mock:
            log.warning("未配置 OPENAI_API_KEY,自动降级为 Mock Provider(仅供流程演示)")
        return MockImageProvider()
    return OpenAIImageProvider()
