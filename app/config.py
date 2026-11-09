"""集中读取环境变量配置。所有配置都可以通过 .env / 环境变量覆盖。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = BASE_DIR / "results"
SAMPLES_DIR = BASE_DIR / "samples"

load_dotenv(BASE_DIR / ".env")


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # 图像 API
    api_key: str = os.getenv("OPENAI_API_KEY", "")
    base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    image_model: str = os.getenv("IMAGE_MODEL", "gpt-image-1")
    image_quality: str = os.getenv("IMAGE_QUALITY", "high")

    # 生成策略
    tile_width: int = int(os.getenv("TILE_WIDTH", "2172"))
    tile_height: int = int(os.getenv("TILE_HEIGHT", "724"))
    overlap_ratio: float = float(os.getenv("OVERLAP_RATIO", "0.25"))
    color_match: bool = _bool("COLOR_MATCH", True)
    # 编辑画布待填区填充:white(兼容性最好)/ transparent(官方 gpt-image 外绘语义)/ mirror(镜像低频提示)
    edit_fill: str = os.getenv("EDIT_FILL", "white")

    # LLM 分镜规划(可选)
    planner_model: str = os.getenv("PLANNER_MODEL", "")

    # 服务
    port: int = int(os.getenv("PORT", "8000"))
    mock: bool = _bool("MOCK", False)

    # 派生
    @property
    def tile_ratio(self) -> float:
        return self.tile_width / self.tile_height


settings = Settings()
