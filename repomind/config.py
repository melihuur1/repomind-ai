"""Settings from the environment, with a project-local ``.env`` as a fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_URL = "postgresql://repomind:repomind@127.0.0.1:55432/repomind"
DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def get_setting(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        value = _read_dotenv(PROJECT_ROOT / ".env").get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str
    embedding_model: str
    models_dir: Path

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            database_url=get_setting("REPOMIND_DATABASE_URL", DEFAULT_DATABASE_URL),
            embedding_model=get_setting("REPOMIND_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            models_dir=Path(get_setting("REPOMIND_MODELS_DIR", str(PROJECT_ROOT / ".models"))),
        )
