"""拼接与融合:编辑画布构建、色彩一致性匹配、重叠区线性羽化拼接。

几何模型:
- 累积图宽度 Wa,新 tile 宽 tile_w,重叠区宽 overlap_px。
- 新 tile 对应全景的列区间 [Wa - overlap_px, Wa - overlap_px + tile_w)。
- 拼接结果宽度 = Wa - overlap_px + tile_w。
- 重叠区内对旧/新像素做线性 cross-fade,过渡带为整个重叠区。
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

from .config import settings


def resize_to(img: Image.Image, w: int, h: int) -> Image.Image:
    if img.size != (w, h):
        img = img.resize((w, h), Image.LANCZOS)
    return img


def build_edit_canvas(accumulated: Image.Image, tile_w: int, overlap_px: int, fill: str = "transparent") -> Image.Image:
    """构造送入 edits API 的画布:左端为已生成图像的右缘锚定条,其余为待补区域。

    fill:
    - transparent:其余区域全透明(gpt-image-1 官方外绘做法,模型会补全透明区)
    - mirror:     其余区域放右缘镜像+高斯模糊,给模型一个低频延续提示
    - white:      其余区域纯白(兼容会把 PNG 压成 JPEG 丢失透明通道的中转)
    """
    h = accumulated.height
    canvas = Image.new("RGBA", (tile_w, h), (0, 0, 0, 0))
    strip = accumulated.crop((accumulated.width - overlap_px, 0, accumulated.width, h))
    canvas.paste(strip, (0, 0))

    if fill == "white":
        blank = Image.new("RGBA", (tile_w - overlap_px, h), (255, 255, 255, 255))
        canvas.paste(blank, (overlap_px, 0))
    elif fill == "mirror":
        tail = accumulated.crop((accumulated.width - (tile_w - overlap_px), 0, accumulated.width, h)) \
            if accumulated.width >= tile_w - overlap_px else accumulated.copy()
        tail = tail.transpose(Image.FLIP_LEFT_RIGHT).resize((tile_w - overlap_px, h), Image.LANCZOS)
        tail = tail.filter(ImageFilter.GaussianBlur(radius=h // 12))
        canvas.paste(tail, (overlap_px, 0))
    # transparent:不做任何填充
    return canvas


def match_color(new_tile: Image.Image, accumulated: Image.Image, overlap_px: int, strength: float = 0.7) -> Image.Image:
    """以重叠区为参照做逐通道均值/方差色彩迁移,抑制相邻 tile 的色调漂移。

    参照 = 累积图右侧 overlap_px 列;被校正 = 新 tile 左侧 overlap_px 列。
    strength=0 不校正,1 完全校正。
    """
    if not settings.color_match or strength <= 0:
        return new_tile
    ref = np.asarray(accumulated.crop((accumulated.width - overlap_px, 0, accumulated.width, accumulated.height)), dtype=np.float32)
    src = np.asarray(new_tile.crop((0, 0, overlap_px, new_tile.height)), dtype=np.float32)
    out = np.asarray(new_tile, dtype=np.float32)

    matched = out.copy()
    for c in range(3):
        m_ref, s_ref = ref[..., c].mean(), ref[..., c].std() + 1e-5
        m_src, s_src = src[..., c].mean(), src[..., c].std() + 1e-5
        gain = float(np.clip(s_ref / s_src, 0.7, 1.4))  # 限制增益,避免极端色彩翻转
        corrected = (out[..., c] - m_src) * gain + m_ref
        matched[..., c] = out[..., c] * (1 - strength) + corrected * strength
    return Image.fromarray(np.clip(matched, 0, 255).astype(np.uint8))


def stitch(accumulated: Image.Image, new_tile: Image.Image, overlap_px: int) -> Image.Image:
    """重叠区线性羽化拼接。"""
    acc = np.asarray(accumulated, dtype=np.float32)
    new = np.asarray(new_tile, dtype=np.float32)
    Wa = acc.shape[1]
    Wn = new.shape[1]
    h = min(acc.shape[0], new.shape[0])

    # 过渡权重:重叠区 x∈[0, overlap_px),alpha 从 0 线性升到 1(旧→新)
    alpha = np.linspace(0.0, 1.0, overlap_px, dtype=np.float32)[None, :, None]

    blended = acc[:, Wa - overlap_px:Wa, :] * (1 - alpha) + new[:, 0:overlap_px, :] * alpha
    out = np.empty((h, Wa - overlap_px + Wn, 3), dtype=np.float32)
    out[:, : Wa - overlap_px] = acc[:, : Wa - overlap_px]
    out[:, Wa - overlap_px:Wa] = blended
    out[:, Wa:] = new[:, overlap_px:]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def crop_final(accumulated: Image.Image, target_w: int) -> Image.Image:
    """最后一块超出目标宽度时,裁掉右端多余部分(不做任何拉伸)。"""
    if accumulated.width > target_w:
        accumulated = accumulated.crop((0, 0, target_w, accumulated.height))
    return accumulated
