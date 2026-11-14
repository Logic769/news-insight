"""Backfill article bodies into insight.db without re-running the model.

Why this exists
----------------
`body` and `body_html` are only populated for articles analyzed after the
columns existed. Every row analyzed before that has `body IS NULL`, which makes
keyword search blind to正文 for the entire back catalogue. Re-running
`analyze --force` would fix it but burns LLM tokens for results we already
have -- the body is a copy of upstream data, not an analysis output.

So this module only moves text: read the crawler API, write `body` and
`body_html`, leave every derived field (category, tags, extractions) untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from app import db
from app.news_client import NewsClient, UpstreamError
from app.pipeline import connect
from app.settings import Settings

LOG = logging.getLogger("news_insight.backfill")

# How many candidates to scan per requested write slot. Rows that cannot be
# filled (dead link, upstream has no body) never leave the candidate set, so
# scanning exactly `limit` rows lets a small budget starve forever: with
# --limit 1 and the newest row broken, every run would retry that one row and
# never reach older articles. Scanning wider decouples "how far we look" from
# "how much we write".
_SCAN_FACTOR = 5

# Upstream says the article is gone. These are permanent, not transient: counting
# them as `failed` would keep `failed > 0` forever and the run would never look
# finished, so operators would retry a dead link indefinitely.
_GONE_STATUS_CODES = frozenset({404, 410})


def _articles_missing_content(conn: Any, scan_budget: int) -> list[dict[str, Any]]:
    """Rows analyzed but missing body text or body HTML, newest first.

    Newest first because that is what users search: the backfill is resumable,
    so running it with a small --limit should fix the most visible rows first.

    The secondary key is not cosmetic: a dead link or an article the upstream
    genuinely has no body for stays in this candidate set forever (we never write
    a placeholder), so the ordering has to be deterministic or the same run would
    pick a different subset each time.

    The second argument is a *scan budget*, not the write limit -- see
    `_SCAN_FACTOR`. The name matters: with `limit` here it is easy to read the
    call site as "look at limit rows" and reintroduce the starvation bug.

    Negative values are clamped to 0: SQLite reads `LIMIT -1` as *no limit*, so a
    caller that bypasses `backfill_bodies` would otherwise get the whole table.
    """
    scan_budget = max(scan_budget, 0)
    rows = conn.execute(
        "SELECT article_id, url FROM analyzed_articles"
        " WHERE body IS NULL OR body = '' OR body_html IS NULL OR body_html = ''"
        " ORDER BY COALESCE(published_at, analyzed_at) DESC, article_id DESC"
        " LIMIT ?",
        (scan_budget,),
    ).fetchall()
    return [dict(r) for r in rows]


def _reject_demo_mode(settings: Settings) -> None:
    """Refuse to backfill in demo mode, which must never touch the network.

    `NewsClient.get_article` goes straight to the live detail endpoint (the demo
    branch only exists in `list_articles`), so calling it here would break the
    contract documented in settings.py: `demo: never touch the network`. Failing
    loudly beats silently writing fixture bodies into a live database.
    """
    if settings.news_source_mode == "demo":
        raise RuntimeError(
            "NEWS_SOURCE_MODE=demo 不允许回填正文：demo 模式不访问网络，"
            "而回填需要逐个请求上游详情接口。请改用 live 或 auto 模式。"
        )


class BackfillUsageError(ValueError):
    """Bad command-line input, reported to the user instead of as a traceback.

    Subclasses ValueError so existing callers that catch ValueError keep working,
    while the CLI can catch just this type: catching bare ValueError there would
    also swallow unrelated internal bugs and turn them into exit code 2 with no
    traceback, which is worse than a crash for something we did not anticipate.
    """


def _validate_limits(*, limit: int, batch: int) -> None:
    """Reject a negative `limit` and a non-positive `batch` before any I/O.

    `limit * _SCAN_FACTOR` on a negative `limit` is still negative, and SQLite
    treats `LIMIT -1` as *no limit* -- so `--limit -1` would quietly backfill the
    entire table, the exact opposite of what the flag asks for. A zero `batch`
    would make `range(0, n, 0)` raise ValueError mid-run.

    `limit=0` is accepted and means "write nothing" (no requests, no writes):
    it is a harmless boundary, unlike a negative one.
    """
    if limit < 0:
        raise BackfillUsageError(
            f"limit 不能为负数（收到 {limit}；SQLite 把 LIMIT -1 当成不限，会回填整库）"
        )
    if batch < 1:
        raise BackfillUsageError(f"batch 必须 >= 1（收到 {batch}）")


def backfill_bodies(settings: Settings, *, limit: int = 500, batch: int = 50) -> dict[str, Any]:
    """Copy bodies and body HTML from the crawler API into insight.db.

    Returns counts rather than raising on individual failures: a dead upstream
    link must not abort a 500-row run, and the caller reruns until `missing`
    stops shrinking.

    `limit` caps how many bodies get written, not how far the run looks: see
    `_SCAN_FACTOR` for why the scan budget is larger. Without that split, a
    broken newest row makes `--limit 1` retry the same row forever.

    Network and writes are strictly separated per chunk: fetch every body in the
    chunk first, then open one short transaction to store them. Interleaving the
    two (`commit=False` accumulating while more HTTP calls go out) holds the
    SQLite write lock across network I/O, so a single slow upstream response
    blocks every other writer until the busy timeout expires.
    """
    _validate_limits(limit=limit, batch=batch)
    _reject_demo_mode(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    conn = connect(settings)
    client = NewsClient(settings)
    filled = 0
    html_filled = 0
    failed = 0
    skipped = 0
    try:
        # `limit` 是写入上限，扫描预算比它大：不可回填的行（死链、上游无正文）
        # 永远留在候选集里，若只扫 limit 行，小的 --limit 会被它们占死——
        # --limit 1 且最新一篇是死链时，每轮都在重试它，更早的文章永远到不了。
        pending = _articles_missing_content(conn, limit * _SCAN_FACTOR)
        LOG.info("待回填正文: 候选 %d 篇，最多写入 %d 篇", len(pending), limit)
        for start in range(0, len(pending), batch):
            if filled >= limit:
                break
            chunk = pending[start : start + batch]
            fetched: list[tuple[str, str, str]] = []
            for row in chunk:
                article_id = row["article_id"]
                try:
                    payload = client.get_article(article_id)
                except UpstreamError as exc:
                    if exc.status_code in _GONE_STATUS_CODES:
                        # 上游明确说这篇不存在（死链），不是临时故障。计成 failed 会让
                        # 「failed > 0 就该重跑」永远成立，运维永远等不到停止信号。
                        LOG.warning("文章已不存在 %s（HTTP %s）", article_id, exc.status_code)
                        skipped += 1
                        continue
                    LOG.warning("取正文失败 %s: %s", article_id, exc)
                    failed += 1
                    continue
                body = (payload or {}).get("raw_content") or (payload or {}).get("content") or ""
                body_html = (payload or {}).get("body_html") or ""
                if not body.strip() and not body_html.strip():
                    # 上游确实没有正文：不算失败，也不写库。代价是这条行仍然是
                    # "缺正文"状态，下一轮还会被选中并再发一次请求——所以
                    # remaining 里始终包含这类不可回填的行，跑几轮都不会归零。
                    # 想永久跳过它们需要一个失败清单，本命令刻意不做（避免在库外
                    # 引入隐式状态）。
                    skipped += 1
                    continue
                fetched.append((article_id, body, body_html))
            # 网络阶段结束，才开写事务；写完立刻提交。
            for article_id, body, body_html in fetched:
                if filled >= limit:
                    # 一个 chunk 内也可能超出 --limit（候选是批量取的），
                    # 超出的留到下一轮，别让一次运行写穿用户给的预算。
                    break
                # set_body/set_body_html 带「只写还没有值的行」守卫，并发下可能
                # 一行都没更新；那种情况不算 filled，否则进度条会比实际好看。
                wrote = False
                if body.strip() and db.set_body(conn, article_id=article_id, body=body, commit=False):
                    wrote = True
                if body_html.strip() and db.set_body_html(
                    conn, article_id=article_id, body_html=body_html, commit=False
                ):
                    html_filled += 1
                    wrote = True
                if wrote:
                    filled += 1
            conn.commit()
            LOG.info("已回填 %d/%d（含 HTML %d，候选 %d）", filled, limit, html_filled, len(pending))
        remaining = conn.execute(
            "SELECT COUNT(*) FROM analyzed_articles"
            " WHERE body IS NULL OR body = '' OR body_html IS NULL OR body_html = ''"
        ).fetchone()[0]
    finally:
        conn.close()
        client.close()
    return {
        "filled": filled,
        "html_filled": html_filled,
        "failed": failed,
        # skipped：上游此刻没有正文，不写库、下一轮还会再试
        "skipped": skipped,
        # remaining：仍然缺正文或正文 HTML 的行数，含 skipped 这类不可回填的，不一定能归零
        "remaining": int(remaining),
    }
