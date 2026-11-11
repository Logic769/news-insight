"""In-process async analyze jobs backed by the analysis_jobs table.

SQLite has one writer, so the executor is deliberately single-threaded: the
pipeline itself fans out LLM calls, while persistence stays on one thread.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from app import db
from app.pipeline import analyze_batch
from app.prune import prune
from app.settings import Settings

LOG = logging.getLogger("news_insight.jobs")


class JobQueueFull(RuntimeError):
    """排队作业达到上限；调用方应返回 429，而不是无限堆任务。"""


# 同步 /v1/analyze 与后台 job 共享：同一时刻只允许一个分析批次，避免重复烧 token。
ANALYZE_LOCK = threading.Lock()


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyze-job")
        self._submit_lock = threading.Lock()

    def start(self) -> None:
        """Mark jobs interrupted by a previous process as failed, not silently stuck."""
        conn = db.connect(self.settings.db_path)
        try:
            count = db.mark_stale_jobs_failed(conn)
            if count:
                LOG.warning("标记 %d 个中断的旧分析任务为 failed", count)
        finally:
            conn.close()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def submit(self, *, limit: int, force: bool, concurrency: int | None = None) -> dict:
        payload = {"limit": limit, "force": force, "concurrency": concurrency}
        with self._submit_lock:
            conn = db.connect(self.settings.db_path)
            try:
                existing = db.find_active_job(conn, payload)
                if existing is not None:
                    return existing
                active = db.count_active_analysis_jobs(conn)
                if active >= self.settings.max_pending_jobs:
                    raise JobQueueFull(
                        f"排队作业已达上限 {self.settings.max_pending_jobs}，请稍后重试"
                    )
                job_id = uuid.uuid4().hex
                db.create_analysis_job(conn, job_id=job_id, payload=payload)
                job = db.get_analysis_job(conn, job_id)
            finally:
                conn.close()
        self._executor.submit(self._run, job_id, limit, force, concurrency)
        assert job is not None
        return job

    def _run(self, job_id: str, limit: int, force: bool, concurrency: int | None) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            db.update_analysis_job(conn, job_id, status="running", started=True)
        finally:
            conn.close()
        try:
            # 不用阻塞等待：同步 /v1/analyze 正在跑长批时，job 若一直等锁会
            # 长时间显示 running。直接失败，语义与同步端点的 409 对齐。
            if not ANALYZE_LOCK.acquire(blocking=False):
                raise JobQueueFull("已有分析任务在运行，请稍后重试")
            try:
                result = analyze_batch(
                    self.settings, limit=limit, force=force, concurrency=concurrency
                )
            finally:
                ANALYZE_LOCK.release()
        except Exception as exc:  # job failure must be visible, never retried silently
            LOG.exception("分析任务失败 %s", job_id)
            conn = db.connect(self.settings.db_path)
            try:
                db.update_analysis_job(
                    conn, job_id, status="failed", error=f"{type(exc).__name__}: {exc}", finished=True
                )
            finally:
                conn.close()
            return
        conn = db.connect(self.settings.db_path)
        try:
            db.update_analysis_job(conn, job_id, status="succeeded", result=result, finished=True)
        finally:
            conn.close()
        # 每批成功后顺手清理过期数据，长驻服务无需等重启
        try:
            prune(self.settings)
        except Exception:
            LOG.exception("批次成功后的定期清理失败")
