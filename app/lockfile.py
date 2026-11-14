"""跨进程文件锁。

分析批次必须全局串行：即便 API 用多个 uvicorn worker 处理读请求，也不能让两个
worker 同时跑 analyze（会重复调用 LLM、重复写库）。线程锁只能在单进程内互斥，
这里再叠加一把基于文件锁的跨进程锁；进程崩溃时操作系统会自动释放文件锁。
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from pathlib import Path
from typing import Any

LOG = logging.getLogger("news_insight.lockfile")


def _get_fcntl() -> Any:
    """返回 fcntl 模块；Windows 上导入失败由调用方回退到 msvcrt。"""
    import fcntl

    return fcntl


def _lock_fd(fd: int, *, blocking: bool) -> None:
    """对已打开的 fd 加排他锁；不支持文件锁时抛 OSError。"""
    try:
        fcntl = _get_fcntl()
    except ImportError:  # pragma: no cover - Windows
        msvcrt: Any = importlib.import_module("msvcrt")

        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(fd, mode, 1)
        return
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    fcntl.flock(fd, flags)


def _unlock_fd(fd: int) -> None:
    try:
        fcntl = _get_fcntl()
    except ImportError:  # pragma: no cover - Windows
        msvcrt: Any = importlib.import_module("msvcrt")

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


class ProcessFileLock:
    """线程锁 + 文件锁的组合锁，接口与 threading.Lock 的常用部分一致。"""

    def __init__(self, filename: str = ".insight.analyze.lock") -> None:
        self._filename = filename
        self._thread_lock = threading.Lock()
        self._fd: int | None = None

    def _path(self) -> Path:
        data_dir = os.environ.get("DATA_DIR") or "/app/data"
        return Path(data_dir) / self._filename

    def acquire(self, blocking: bool = False) -> bool:
        # blocking=True 会无限等待；生产路径一律用 False（拿不到就快速失败）。
        if not self._thread_lock.acquire(blocking=blocking):
            return False
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                _lock_fd(fd, blocking=blocking)
            except OSError:
                os.close(fd)
                self._thread_lock.release()
                return False
            self._fd = fd
            return True
        except OSError as exc:
            LOG.warning("无法创建分析锁文件 %s: %s", self._path(), exc)
            self._thread_lock.release()
            return False

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                _unlock_fd(fd)
            except OSError:
                LOG.exception("释放分析锁文件失败")
            finally:
                os.close(fd)
        try:
            self._thread_lock.release()
        except RuntimeError:
            pass
