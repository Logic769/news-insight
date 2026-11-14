"""Runtime settings, loaded from environment variables only.

Secrets never live in the image: every LLM/news credential is injected at
`docker compose` time through the whitelisted environment block.
"""

from __future__ import annotations

import logging
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


def _env_int_range(name: str, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, _env_int(name, default)))


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
    # 供应商侧同时请求上限：分析并发高于它时，多出的线程在本地排队，避免瞬时打爆网关。
    llm_max_concurrency: int = field(default_factory=lambda: _env_int_range("LLM_MAX_CONCURRENCY", 100, 1, 200))

    # --- inbound API auth ------------------------------------------------
    api_key: str = field(default_factory=lambda: _env("INSIGHT_API_KEY"))
    require_auth: bool = field(default_factory=lambda: _env("INSIGHT_REQUIRE_AUTH", "0") == "1")

    # --- local accounts --------------------------------------------------
    # 首次启动、users 表为空时创建的唯一管理员；密码只从环境变量读取。
    admin_username: str = field(default_factory=lambda: _env("INSIGHT_ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: _env("INSIGHT_ADMIN_PASSWORD"))
    session_ttl_days: int = field(default_factory=lambda: _env_int_range("INSIGHT_SESSION_TTL_DAYS", 7, 1, 365))

    # --- analysis behaviour ---------------------------------------------
    max_tags_per_article: int = field(default_factory=lambda: _env_int("MAX_TAGS_PER_ARTICLE", 5))
    tag_body_chars: int = field(default_factory=lambda: _env_int("TAG_BODY_CHARS", 1200))
    extract_body_chars: int = field(default_factory=lambda: _env_int("EXTRACT_BODY_CHARS", 6000))
    # 单进程内并发分析的文章数；每篇内部仍串行调用 3 次 LLM。
    analyze_concurrency: int = field(default_factory=lambda: _env_int_range("ANALYZE_CONCURRENCY", 100, 1, 200))
    # 结果按批提交（主线程唯一写者），减少 SQLite 事务次数。
    analyze_write_batch: int = field(default_factory=lambda: _env_int_range("ANALYZE_WRITE_BATCH", 50, 1, 500))
    # 上游详情页并发拉取数（1 万篇时串行拉正文会成为瓶颈）。
    news_detail_concurrency: int = field(default_factory=lambda: _env_int_range("NEWS_DETAIL_CONCURRENCY", 16, 1, 64))
    # 分析结果保留天数；0 表示不自动清理。
    retention_days: int = field(default_factory=lambda: _env_int_range("RETENTION_DAYS", 90, 0, 3650))
    # 异步作业排队上限（queued + running）：超过后 POST /v1/jobs 返回 429。
    max_pending_jobs: int = field(default_factory=lambda: _env_int_range("ANALYZE_MAX_PENDING_JOBS", 50, 1, 1000))

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
    settings = Settings()
    if not (settings.api_key or settings.require_auth):
        logging.getLogger("news_insight.settings").warning(
            "未配置 INSIGHT_API_KEY：/v1 端点对所有能访问本端口的人开放（POST /v1/analyze 会消耗 LLM 额度）"
        )
    return settings
