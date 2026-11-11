"""Runtime settings, loaded from environment variables only.

Secrets never live in the image: every LLM/news credential is injected at
`docker compose` time through the whitelisted environment block.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- upstream news-crawler /v1 -------------------------------------
    news_base_url: str = field(default_factory=lambda: _env("NEWS_BASE_URL", "http://host.docker.internal:8000").rstrip("/"))
    news_api_key: str = field(default_factory=lambda: _env("NEWS_API_KEY"))
    # auto: try the live API, fall back to bundled fixtures when unreachable.
    # live: fail loudly. demo: never touch the network.
    news_source_mode: str = field(default_factory=lambda: _env("NEWS_SOURCE_MODE", "auto").lower())
    news_page_size: int = field(default_factory=lambda: _env_int("NEWS_PAGE_SIZE", 20))
    news_timeout: float = field(default_factory=lambda: _env_float("NEWS_TIMEOUT", 15.0))

    # --- LLM ------------------------------------------------------------
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-chat"))
    llm_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 60.0))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.0))

    # --- analysis behaviour ---------------------------------------------
    max_tags_per_article: int = field(default_factory=lambda: _env_int("MAX_TAGS_PER_ARTICLE", 5))
    tag_body_chars: int = field(default_factory=lambda: _env_int("TAG_BODY_CHARS", 1200))
    extract_body_chars: int = field(default_factory=lambda: _env_int("EXTRACT_BODY_CHARS", 6000))
    analyze_batch_size: int = field(default_factory=lambda: _env_int("ANALYZE_BATCH_SIZE", 10))

    # --- storage --------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/app/data")))
    fixtures_dir: Path = field(default_factory=lambda: Path(_env("FIXTURES_DIR", "")) if _env("FIXTURES_DIR") else Path(__file__).resolve().parent.parent / "fixtures")
    vocab_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "vocab")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "insight.db"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)


def load_settings() -> Settings:
    return Settings()
