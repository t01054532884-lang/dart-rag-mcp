"""경로·환경 변수 설정. 키는 .env 또는 환경 변수에서만 읽는다."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-large"  # 512토큰. 빠른 실험용: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2


def model_slug(model: str) -> str:
    """임베딩 모델마다 색인 폴더를 따로 둔다 (모델을 바꿔 비교해도 이전 색인이 남음)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model.split("/")[-1])


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """간단한 .env 로더 (이미 설정된 환경 변수는 덮어쓰지 않음)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    qdrant_path: Path
    raw_dir: Path
    embed_model: str
    hybrid_bm25_weight: float
    dart_api_key: str | None


def get_settings() -> Settings:
    load_dotenv()
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    embed_model = os.environ.get("EMBED_MODEL", DEFAULT_EMBED_MODEL)
    return Settings(
        data_dir=data_dir,
        db_path=data_dir / "fin.db",
        qdrant_path=data_dir / "qdrant" / model_slug(embed_model),
        raw_dir=data_dir / "raw",
        embed_model=embed_model,
        hybrid_bm25_weight=float(os.environ.get("HYBRID_BM25_WEIGHT", "0.7")),
        dart_api_key=os.environ.get("DART_API_KEY") or None,
    )
