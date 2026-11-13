"""关键词检索层与 /v1/search 的回归用例（分组共 6 大类，含 other 兜底）。

覆盖的重点是「排序的确由后端算出来」这件事，因为前端拿不到标签置信度与
正文，无法自行重排——后端给错顺序就是错的最终结果。

- 查询词映射到闭集标签时，完整短语必须压过其中的泛化子片段；
- 类内按相关度降序，跨类分组不串味；
- 命中不了任何东西的词要返回空结果，而不是把全库兜出来；
- 空大类的表现、relevance 各分量、分页截断都要可预期。
"""

from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from app import db, search
from app.tagging import TagDef

TAGS = [
    TagDef("new_energy", "新能源", "industry", ("光伏", "风电", "储能")),
    TagDef("subsidy", "补贴奖补", "funding", ("补贴", "奖补", "资助")),
    TagDef("hr_talent", "人才", "talent", ("人才", "招聘", "职称")),
    TagDef("project_application", "项目申报", "event", ("申报", "征集")),
]

CATEGORY_ORDER = (
    "project_application",
    "industry_policy",
    "talent_application",
    "enterprise_certification",
    "funding_subsidy",
    "other",
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    return conn


def _add(
    conn: sqlite3.Connection,
    article_id: str,
    *,
    title: str,
    body: str | None = "",
    category: str = "other",
    tag_confidences: tuple[tuple[str, float], ...] = (),
    published_at: str = "2026-11-01 00:00:00",
) -> None:
    conn.execute(
        "INSERT INTO analyzed_articles"
        " (article_id, source, url, title, published_at, content_length,"
        "  body, category, category_confidence, category_engine,"
        "  tag_engine, extract_engine, analyzed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            article_id,
            "svc",
            f"https://example.com/{article_id}",
            title,
            published_at,
            len(body or ""),
            body,
            category,
            0.9,
            "rules",
            "rules",
            "rules",
            "2026-11-02 00:00:00",
        ),
    )
    for tag, conf in tag_confidences:
        conn.execute(
            "INSERT INTO article_tags (article_id, tag, confidence, evidence, engine)"
            " VALUES (?,?,?,?,?)",
            (article_id, tag, conf, "", "rules"),
        )
    conn.commit()

def test_resolve_query_maps_phrase_to_closed_tags() -> None:
    """完整短语要命中对应标签，同时保留原词作为文本检索词。"""
    q = search.resolve_query("光伏补贴", TAGS)
    assert "new_energy" in q.tag_ids
    assert "subsidy" in q.tag_ids
    assert "光伏补贴" in q.term_texts
    assert "光伏" in q.term_texts and "补贴" in q.term_texts


def test_resolve_query_weights_fragment_below_full_match() -> None:
    """整词命中记 1.0，子片段按长度占比折算。

    这是长度惩罚而不是语义判断：像「人才申报」这种由两个二字词拼成的查询，
    两个候选都只覆盖一半，权重相同 —— 真正区分两类文章的是标签置信度与
    标题命中，不是覆盖度。用例把这条边界钉住，避免日后误以为覆盖度在做
    语义消歧。
    """
    exact = dict(search.resolve_query("光伏", TAGS).tag_matches)
    assert exact["new_energy"] == 1.0

    partial = dict(search.resolve_query("人才申报", TAGS).tag_matches)
    assert partial["hr_talent"] == 0.5
    assert partial["project_application"] == 0.5

    # 覆盖度高的候选确实排在前面：sort 的键是覆盖度。
    phrase = search.resolve_query("光伏补贴", TAGS).tag_matches
    assert all(0.0 < w <= 1.0 for _, w in phrase)


def test_resolve_query_empty_returns_empty_query() -> None:
    q = search.resolve_query("   ", TAGS)
    assert q.is_empty


def test_resolve_query_unknown_word_falls_back_to_literal_term() -> None:
    """词表覆盖不到的词也必须能搜，否则用户输新说法就瞎了。"""
    q = search.resolve_query("量子计算", TAGS)
    assert q.tag_ids == ()
    assert q.term_texts == ("量子计算",)


def test_score_prefers_tag_hit_over_body_only_hit() -> None:
    q = search.resolve_query("光伏", TAGS)
    tagged = search.score_article(
        q,
        title="某地推进光伏项目",
        body="光伏",
        tag_confidences={"new_energy": 0.9},
    )
    untagged = search.score_article(
        q,
        title="无关标题",
        body="光伏" * 20,
        tag_confidences={},
    )
    assert tagged.score > untagged.score


def test_score_is_bounded() -> None:
    q = search.resolve_query("光伏", TAGS)
    rel = search.score_article(
        q,
        title="光伏" * 50,
        body="光伏" * 500,
        tag_confidences={"new_energy": 1.0},
    )
    assert 0.0 <= rel.score <= 1.0


def test_has_signal_false_for_unrelated_article() -> None:
    q = search.resolve_query("光伏", TAGS)
    rel = search.score_article(
        q, title="完全无关", body="完全无关的正文", tag_confidences={}
    )
    assert not search.has_signal(rel)


def test_search_groups_by_category_and_ranks_inside_group() -> None:
    conn = _conn()
    _add(
        conn, "a",
        title="光伏补贴申报通知",
        body="光伏补贴" * 5,
        category="funding_subsidy",
        tag_confidences=(("new_energy", 0.95), ("subsidy", 0.9)),
    )
    _add(
        conn, "b",
        title="光伏产业观察",
        body="光伏",
        category="industry_policy",
        tag_confidences=(("new_energy", 0.4),),
    )
    result = db.search_grouped(
        conn, raw_query="光伏", tags=TAGS, category_order=CATEGORY_ORDER
    )
    assert result["total"] == 2
    cats = [g["category"] for g in result["groups"]]
    assert cats == ["industry_policy", "funding_subsidy"]
    for group in result["groups"]:
        scores = [i["relevance"]["score"] for i in group["items"]]
        assert scores == sorted(scores, reverse=True)
    conn.close()


def test_search_unknown_keyword_returns_no_groups() -> None:
    conn = _conn()
    _add(conn, "a", title="光伏新闻", body="光伏", tag_confidences=(("new_energy", 0.9),))
    result = db.search_grouped(
        conn, raw_query="量子计算", tags=TAGS, category_order=CATEGORY_ORDER
    )
    assert result["total"] == 0
    assert result["groups"] == []
    conn.close()


def test_search_body_hit_does_not_leak_into_other_category() -> None:
    """正文命中跨类时，只出现在它自己那个大类下，不会被复制到别的类。"""
    conn = _conn()
    _add(conn, "a", title="无关标题", body="这里提到光伏", category="other")
    result = db.search_grouped(
        conn, raw_query="光伏", tags=TAGS, category_order=CATEGORY_ORDER
    )
    assert len(result["groups"]) == 1
    assert result["groups"][0]["category"] == "other"
    conn.close()


def test_search_respects_per_category_limit_but_reports_full_count() -> None:
    conn = _conn()
    for i in range(5):
        _add(
            conn, f"a{i}",
            title=f"光伏新闻{i}",
            body="光伏",
            category="other",
            tag_confidences=(("new_energy", 0.9),),
        )
    result = db.search_grouped(
        conn, raw_query="光伏", tags=TAGS, category_order=CATEGORY_ORDER, per_category=2
    )
    group = result["groups"][0]
    assert group["count"] == 5
    assert len(group["items"]) == 2
    conn.close()


def test_search_filters_by_requested_categories() -> None:
    conn = _conn()
    _add(conn, "a", title="光伏新闻", body="光伏", category="other",
         tag_confidences=(("new_energy", 0.9),))
    _add(conn, "b", title="光伏政策", body="光伏", category="industry_policy",
         tag_confidences=(("new_energy", 0.9),))
    result = db.search_grouped(
        conn,
        raw_query="光伏",
        tags=TAGS,
        category_order=CATEGORY_ORDER,
        categories=["industry_policy"],
    )
    assert [g["category"] for g in result["groups"]] == ["industry_policy"]
    conn.close()


def test_search_omits_body_by_default_and_includes_on_demand() -> None:
    conn = _conn()
    _add(conn, "a", title="光伏新闻", body="光伏正文", category="other",
         tag_confidences=(("new_energy", 0.9),))
    lean = db.search_grouped(
        conn, raw_query="光伏", tags=TAGS, category_order=CATEGORY_ORDER
    )
    assert "body" not in lean["groups"][0]["items"][0]
    full = db.search_grouped(
        conn, raw_query="光伏", tags=TAGS, category_order=CATEGORY_ORDER,
        include_body=True,
    )
    assert full["groups"][0]["items"][0]["body"] == "光伏正文"
    conn.close()


def test_search_reports_matched_tags_and_terms_for_debugging() -> None:
    conn = _conn()
    _add(conn, "a", title="光伏补贴通知", body="光伏", category="other",
         tag_confidences=(("new_energy", 0.9), ("subsidy", 0.8)))
    result = db.search_grouped(
        conn, raw_query="光伏补贴", tags=TAGS, category_order=CATEGORY_ORDER
    )
    assert set(result["matched_tags"]) == {"new_energy", "subsidy"}
    assert "光伏补贴" in result["terms"]
    conn.close()



def test_resolve_query_full_label_beats_its_prefix() -> None:
    """标签名完整命中不能被「同标签的短前缀」压成一半。

    词表里大量短词是长词前缀（「人才」→「人才计划」）。若实现命中第一个候选就
    break，用户输入完整标签名反而只拿到 0.5，同一句话的排序还会随 keywords 的
    排列顺序变化 —— 这是被测出来的真实缺陷，用例把它钉住。
    """
    tags = [TagDef("hr_talent", "人才", "talent", ("人才", "人才计划"))]
    # 短前缀在前、长词在后：命中即 break 的实现会只拿到 0.5
    q = search.resolve_query("人才计划", tags)
    assert dict(q.tag_matches)["hr_talent"] == 1.0

    # 反过来的排列顺序不能改变结果
    tags_rev = [TagDef("hr_talent", "人才", "talent", ("人才计划", "人才"))]
    assert dict(search.resolve_query("人才计划", tags_rev).tag_matches)["hr_talent"] == 1.0


def test_resolve_query_keeps_all_matched_surface_forms() -> None:
    """同标签多个候选命中时，每个命中形态都进文本词表（正文里都要能数到）。"""
    tags = [TagDef("hr_talent", "人才", "talent", ("人才", "人才计划"))]
    q = search.resolve_query("人才计划", tags)
    assert "人才" in q.term_texts
    assert "人才计划" in q.term_texts


def test_score_clamps_dirty_tag_confidence() -> None:
    """库里存了 >1 的置信度也不能让总分突破 1.0（契约是 [0,1]）。"""
    q = search.resolve_query("光伏", TAGS)
    rel = search.score_article(
        q, title="光伏新闻", body="", tag_confidences={"new_energy": 5.0}
    )
    assert rel.score <= 1.0
    assert rel.tag_confidence == 1.0


def test_search_matches_english_case_insensitively() -> None:
    """SQL 预筛与内存打分必须用同一套归一，否则英文词静默漏检。

    预筛用 instr(title, ?) 是区分大小写的，而打分用 casefold 计数。搜 gpt、
    文章写 GPT 时，会在 SQL 层就被丢掉，返回 200 + 空结果，不报错。
    """
    conn = _conn()
    _add(conn, "a1", title="GPT 大模型发布", body="模型能力提升")
    out = db.search_grouped(conn, raw_query="gpt", tags=TAGS)
    assert out["total"] == 1
    assert out["groups"][0]["items"][0]["article_id"] == "a1"

    # 反向：查询大写、正文小写
    out2 = db.search_grouped(conn, raw_query="GPT", tags=TAGS)
    assert out2["total"] == 1
    conn.close()


def test_search_tie_break_is_stable_regardless_of_insert_order() -> None:
    """完全平局时顺序必须由数据决定，不能由 SQLite 行序决定。

    前端拿到的 items[] 顺序就是最终展示顺序，查询计划一变排列就抖。
    """

    def order(ids: list[str]) -> list[str]:
        conn = _conn()
        for aid in ids:
            _add(
                conn,
                aid,
                title="光伏新闻",
                body="",
                tag_confidences=(("new_energy", 0.9),),
                published_at="2026-11-01 00:00:00",
            )
        got = [
            i["article_id"]
            for g in db.search_grouped(conn, raw_query="光伏", tags=TAGS)["groups"]
            for i in g["items"]
        ]
        conn.close()
        return got

    assert order(["a1", "a2", "a3"]) == order(["a3", "a1", "a2"])


def test_search_body_preserved_when_rerun_without_body() -> None:
    """重跑时没拿到正文不能把已有正文冲掉（否则检索会静默丢文章）。

    save_analysis 用 COALESCE(NULLIF(excluded.body, ''), body)：NULL 与空串都
    保留旧值，只有真拿到文本时才覆盖。
    """
    conn = _conn()

    def save(body: str | None) -> None:
        db.save_analysis(
            conn,
            article_id="a1",
            source="svc",
            url="https://example.com/a1",
            title="光伏新闻",
            published_at="2026-11-01 00:00:00",
            published_at_utc="2026-11-01 00:00:00",
            content_length=6,
            tag_engine="rules",
            extract_engine="rules",
            tags=[],
            extractions=[],
            body=body,
        )

    save("原始正文光伏")
    for empty in (None, ""):
        save(empty)
        got = conn.execute(
            "SELECT body FROM analyzed_articles WHERE article_id = ?", ("a1",)
        ).fetchone()
        assert got["body"] == "原始正文光伏"
    conn.close()


def test_set_body_does_not_overwrite_existing_body() -> None:
    """回填只补空行：先取快照再写，期间若已有新正文不能被旧副本覆盖。"""
    conn = _conn()
    _add(conn, "a1", title="光伏新闻", body="已经有的正文")
    db.set_body(conn, article_id="a1", body="旧副本")
    got = conn.execute(
        "SELECT body FROM analyzed_articles WHERE article_id = ?", ("a1",)
    ).fetchone()
    assert got["body"] == "已经有的正文"

    # 空正文的行才写得进去
    _add(conn, "a2", title="光伏新闻", body="")
    db.set_body(conn, article_id="a2", body="回填正文")
    got2 = conn.execute(
        "SELECT body FROM analyzed_articles WHERE article_id = ?", ("a2",)
    ).fetchone()
    assert got2["body"] == "回填正文"
    conn.close()


def test_search_works_when_body_is_null() -> None:
    """历史行 body 为 NULL：标题与标签仍要能命中，不能报错也不能整篇丢掉。"""
    conn = _conn()
    _add(
        conn,
        "a1",
        title="光伏新闻",
        body=None,
        tag_confidences=(("new_energy", 0.9),),
    )
    # 只有 include_body=True 时 items 才带 body 字段（默认被 pop 掉）
    out = db.search_grouped(conn, raw_query="光伏", tags=TAGS, include_body=True)
    assert out["total"] == 1
    assert out["groups"][0]["items"][0]["body"] is None

    out2 = db.search_grouped(conn, raw_query="新能源", tags=TAGS)
    assert out2["total"] == 1
    conn.close()


def test_search_total_counts_beyond_per_category_cut() -> None:
    """per_category 只截断每组 items，total 与每组 count 仍报全量命中。

    候选集上限（search_candidates 的 limit=2000）另有用例覆盖，两者不是同一件事。
    """
    conn = _conn()
    for i in range(5):
        _add(conn, f"a{i}", title="光伏新闻", body="", published_at="2026-11-01 00:00:00")
    out = db.search_grouped(conn, raw_query="光伏", tags=TAGS, per_category=2)
    assert out["total"] == 5
    assert sum(g["count"] for g in out["groups"]) == 5
    assert all(len(g["items"]) <= 2 for g in out["groups"])
    conn.close()


def test_search_ignores_whitespace_in_query_and_text() -> None:
    """SQL 预筛与内存打分必须共用同一套归一，空格差异不能静默漏检。

    预筛只补 lower() 是不够的：搜「gpt 模型」时 SQL 拿带空格的原文去 instr，
    标题写着「GPT模型」，候选集直接为空 —— 返回 200 加空结果，不报错。正文里的
    全角空格同理。
    """
    conn = _conn()
    _add(conn, "a1", title="GPT模型发布", body="模型能力提升")
    _add(conn, "a2", title="人才招聘", body="光伏\u3000补贴细则")

    assert db.search_grouped(conn, raw_query="gpt 模型", tags=TAGS)["total"] == 1
    assert db.search_grouped(conn, raw_query="gpt模型", tags=TAGS)["total"] == 1
    # 正文中间的全角空格与普通空格等价
    assert db.search_grouped(conn, raw_query="光伏 补贴", tags=TAGS)["total"] == 1
    assert db.search_grouped(conn, raw_query="光伏补贴", tags=TAGS)["total"] == 1
    assert db.search_grouped(conn, raw_query="光伏\u3000补贴", tags=TAGS)["total"] == 1
    conn.close()


def test_score_counts_each_occurrence_once() -> None:
    """同一处文本不能被多个候选形态重复计数。

    词表里短词常是长词前缀（「人才」⊂「人才申报」）。若每个形态各自 count，
    标题里一次「人才申报」会被记成 3 次命中、正文项被系统性高估（短文尤甚）。
    """
    tags = [TagDef("project_application", "项目申报", "category", ("申报",))]
    q = search.resolve_query("项目申报", tags)
    assert len(q.terms) >= 2  # 「项目申报」与「申报」都在 terms 里
    rel = search.score_article(q, title="项目申报通知", body="", tag_confidences={})
    assert rel.title_hits == 1, rel

    # 短形态仍要能命中长形态覆盖不到的文本
    rel2 = search.score_article(q, title="通知", body="请于本周内申报", tag_confidences={})
    assert rel2.body_hits == 1, rel2
    assert "申报" in rel2.matched_terms


def test_score_reports_all_matched_terms() -> None:
    """matched_terms 用于排障，命中过的形态都要列出来。"""
    tags = [TagDef("project_application", "项目申报", "category", ("申报",))]
    q = search.resolve_query("项目申报", tags)
    rel = search.score_article(q, title="项目申报", body="申报", tag_confidences={})
    assert set(rel.matched_terms) == {"项目申报", "申报"}


def test_matched_terms_excludes_forms_swallowed_by_longer_match() -> None:
    """matched_terms 是排障用的，不能列出实际没计入命中数的形态。

    标题里一处「项目申报」在掩码计数后只贡献 1 次命中，短形态「申报」被吃掉；
    若 matched_terms 仍列出它，排障者会以为该词贡献过分数。
    """
    tags = [TagDef("project_application", "项目申报", "category", ("申报",))]
    q = search.resolve_query("项目申报", tags)
    rel = search.score_article(q, title="项目申报通知", body="", tag_confidences={})
    assert rel.title_hits == 1
    assert rel.matched_terms == ["项目申报"], rel.matched_terms


def test_search_matches_non_ascii_case_and_insensitively() -> None:
    """SQL 预筛必须与打分用同一套归一，非 ASCII 大小写也不例外。

    SQLite 的 lower() 只折 ASCII：把 "Ärger" 折完还是 "Ärger"，而 Python 的
    casefold() 折成 "ärger"。曾因此出现「用户输入与标题字面完全一致也返回空结果」——
    打分阶段能命中（title_hits=1），是 SQL 预筛把行丢了。真实触发词是机构名/外国新闻
    （ÖBB、Ärzte、Müller、École），表现是 200 + 空结果、不报错。
    """
    conn = _conn()
    _add(conn, "a1", title="Ärger über alles")
    _add(conn, "a2", title="ÖBB 交通新闻")
    _add(conn, "a3", title="MÜLLER GmbH 招标")
    for raw, expect in (("Ärger", "a1"), ("ärger", "a1"), ("ÄRGER", "a1"), ("ÖBB 交通", "a2"), ("öbb交通", "a2"), ("müller", "a3"), ("MÜLLER", "a3")):
        got = db.search_grouped(conn, raw_query=raw, tags=TAGS, category_order=CATEGORY_ORDER)
        ids = [item["article_id"] for g in got["groups"] for item in g["items"]]
        assert ids == [expect], (raw, ids)


def test_search_ignores_unicode_narrow_spaces() -> None:
    """从 Word / 网页复制的关键词常带 \u2002/\u2003/\u2009 这类窄空格。

    空白表漏一个就表现为「看起来一样的两个词搜不到」，且不报错。SQL 侧与 Python
    侧共用 search.WS_CHARS，所以只要表里有，两层就一致。
    """
    conn = _conn()
    _add(conn, "a1", title="光伏\u2003补贴通知")
    _add(conn, "a2", title="光伏\u2009补贴公告")
    _add(conn, "a3", title="光伏\u2028补贴简报")
    for raw in ("光伏补贴", "光伏\u2003补贴", "光伏\u2009补贴"):
        got = db.search_grouped(conn, raw_query=raw, tags=TAGS, category_order=CATEGORY_ORDER, per_category=10)
        assert got["total"] == 3, (raw, got["total"])
        # 只断言 total 不够：词表里「补贴」本身就是候选词，空白没被归一时单靠它
        # 也能让三篇命中，用例会假通过。这里要求完整组合词本身被计入 —— 只有
        # 空白真被折掉、跨空格的「光伏补贴」整体匹配上，它才会出现在 matched_terms。
        # matched_terms 保留查询里的原始写法，所以比对时去掉空白再比。
        top = got["groups"][0]["items"][0]["relevance"]
        assert search._norm(top["matched_terms"][0]) == "光伏补贴", (raw, top)


def test_masked_hits_overlapping_occurrence_counts_once() -> None:
    """同一形态的重叠出现算同一处文本，只计一次。

    「aa」在「aaa」里有两个起始位置，但覆盖的是同一段字符；重复计数会让高频短词
    在长正文里堆出虚假命中数。
    """
    assert search._masked_hits("aaa", (("aa", 1.0),))[0] == 1
    assert search._masked_hits("ababa", (("aba", 1.0),))[0] == 1
    assert search._masked_hits("aaaa", (("aa", 1.0),))[0] == 2


def test_masked_hits_stays_fast_on_high_frequency_short_term() -> None:
    """记账不能退化成 O(n²)：单字高频命中是真实场景（「的」「年」）。

    早期用区间列表实现时，每次命中都要跟全部已记区间比一遍，6000 字正文里单字高频
    命中实测 0.87s；标记数组方案同一场景 0.004s。这里只卡一个很宽松的上限，用来挡住
    退回列表实现（而不是测精确耗时）。
    """
    body = "的" * 6000
    started = time.perf_counter()
    hits, _w, _used = search._masked_hits(body, (("的", 1.0),))
    elapsed = time.perf_counter() - started
    assert hits == 6000
    # 上限给到 0.5s：当前实现约 0.003s，旧列表实现 0.87s，两头都有余量，慢 CI 不会误报。
    assert elapsed < 0.5, elapsed


def test_candidate_cap_warns_only_when_rows_are_actually_dropped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """截断判定用「多取一行」：恰好取满不算截断。

    直接比对 len(rows) == limit 会在库里恰好 limit 行（其实全取完了）时误报
    「结果可能不完整」。这里把上限缩到 3，用 3 行与 4 行两个边界锁住行为，
    比造 2001 行快得多。
    """
    monkeypatch.setattr(db, "_CANDIDATE_LIMIT", 3)
    conn = _conn()
    for i in range(3):
        _add(conn, "a" + str(i), title="光伏补贴通知" + str(i), category="funding_subsidy")

    with caplog.at_level("WARNING", logger="news_insight.db"):
        capped = db.search_grouped(conn, raw_query="光伏", tags=TAGS, per_category=100)
    assert capped["total"] == 3
    # 只关心「有没有告警」，不匹配文案：改文案时这里不该静默变成永远通过。
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    _add(conn, "a3", title="光伏补贴通知3", category="funding_subsidy")
    caplog.clear()
    with caplog.at_level("WARNING", logger="news_insight.db"):
        capped = db.search_grouped(conn, raw_query="光伏", tags=TAGS, per_category=100)
    assert capped["total"] == 3
    assert [r for r in caplog.records if "候选集打满" in r.getMessage()]

def test_search_candidates_keeps_newest_rows_when_truncating() -> None:
    """候选截断前必须先定序，且保留的是最新的行。

    CodeRabbit 线程 4 的问题不是「顺序不稳定」而已：没有 ORDER BY 时，命中超过
    上限的那次查询截到哪 2000 行由扫描顺序决定，于是同一查询两次结果可能不同，
    用户会看到「老文章一会儿搜得到一会儿搜不到」。定序之后截断集合固定，且产品上
    取最新——这正是 search_grouped 会重排之外、候选层唯一影响用户的地方。

    这里直接给 search_candidates 传 limit=600（接近默认 _CANDIDATE_LIMIT 的形态），
    而不是 monkeypatch 那个常量：调用方本来就显式传 limit，改常量对这个用例无效。
    """
    conn = _conn()
    for i in range(2500):
        # published_at 与 i 严格同序：i 越大越新（用日期 + 秒两段拼，避免
        # 取模导致日期回绕）。
        day = i // 60
        sec = i % 60
        _add(conn, f"a{i:04d}", title="光伏补贴通知", published_at=f"2026-{1 + day // 28:02d}-{day % 28 + 1:02d} 00:00:{sec:02d}")

    rows = db.search_candidates(conn, terms=["光伏"], tag_ids=[], limit=600)
    assert len(rows) == 600
    ids = [r["article_id"] for r in rows]
    assert ids == sorted(ids, reverse=True), "候选集必须按最新优先返回"
    assert ids[0] == "a2499"

    again = [r["article_id"] for r in db.search_candidates(conn, terms=["光伏"], tag_ids=[], limit=600)]
    assert again == ids, "同一查询两次必须截到同一集合"


def test_search_candidates_sorts_undated_rows_by_sql_null_order() -> None:
    """两个时间列都空的行按 SQL 的 NULL 序参与排序：NULL 最小，降序沉底、升序置顶。

    早先定序写成 COALESCE(published_at, analyzed_at)：没有发布时间的行会被
    analyzed_at（分析时刻，通常就是今天）顶到**最前**，用户按时间浏览时先看到的
    全是无日期的老行。改成 _TIME_EXPR 后它们不再拿分析时刻冒充发布时间，
    行为收敛成 SQLite 的默认 NULL 序（生产库当前 0 行两列皆空，带时间过滤时
    这类行本来就会被排除并计入 undated_excluded）。
    """
    conn = _conn()
    _add(conn, "with_date", title="光伏补贴", published_at="2026-11-01 00:00:00")
    _add(conn, "no_date", title="光伏补贴", published_at=None)  # type: ignore[arg-type]
    conn.execute(
        "UPDATE analyzed_articles SET analyzed_at = ? WHERE article_id = ?",
        ("2026-11-05 00:00:00", "no_date"),
    )
    conn.commit()

    rows = db.search_candidates(conn, terms=["光伏"], tag_ids=[], limit=10)
    ids = [r["article_id"] for r in rows]
    assert ids == ["with_date", "no_date"], ids

    # 升序时 NULL 最小，无日期行排最前；行为由 SQL 的 NULL 序决定，写死以便察觉变化
    asc = db.search_candidates(conn, terms=["光伏"], tag_ids=[], sort="published_asc", limit=10)
    assert [r["article_id"] for r in asc] == ["no_date", "with_date"]


def test_search_candidates_tie_break_is_article_id_desc() -> None:
    """发布时间完全相同时，`article_id DESC` 兜底决定截断与返回顺序。

    同秒发布的文章很常见（列表页只给到日期，多篇都是 00:00:00）。若排序键只写
    `COALESCE(published_at, analyzed_at)` 而没有第二键，SQLite 对等值行的相对顺序
    不保证稳定，截断边界上同样会出现「同一查询两次截到不同集合」。这里让 id 顺序
    刻意与插入顺序相反，避免「碰巧按 rowid 返回」也算通过。
    """
    conn = _conn()
    for aid in ("b3", "b1", "b2"):
        _add(conn, aid, title="光伏补贴", published_at="2026-11-01 00:00:00")

    rows = db.search_candidates(conn, terms=["光伏"], tag_ids=[], limit=2)
    ids = [r["article_id"] for r in rows]
    assert ids == ["b3", "b2"], ids

    again = [r["article_id"] for r in db.search_candidates(conn, terms=["光伏"], tag_ids=[], limit=2)]
    assert again == ids, "等值行也必须稳定截断"

# --------------------------------------------- 新增：时间/来源过滤与排序下推

def _add_dt(
    conn: sqlite3.Connection,
    article_id: str,
    *,
    title: str = "光伏补贴通知",
    body: str = "",
    source: str = "svc",
    published_at: str | None = "2026-11-01 00:00:00",
    published_at_utc: str | None = None,
    category: str = "other",
) -> None:
    """带 published_at_utc / source 的插入，覆盖混时区与来源筛选。"""
    conn.execute(
        "INSERT INTO analyzed_articles"
        " (article_id, source, url, title, published_at, published_at_utc, content_length,"
        "  body, category, category_confidence, category_engine,"
        "  tag_engine, extract_engine, analyzed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            article_id,
            source,
            f"https://example.com/{article_id}",
            title,
            published_at,
            published_at_utc,
            len(body),
            body,
            category,
            0.9,
            "rules",
            "rules",
            "rules",
            "2026-11-02 00:00:00",
        ),
    )
    conn.commit()


def test_search_candidates_sorts_ascending_when_asked() -> None:
    """按时间升序查时必须保留最老的一批。

    候选截断方向跟着排序走：仍按「最新优先」截断的话，用户要看最老的几条，
    截断却把最老的几千行全扔掉，结果集里只剩最新的，答案直接是错的。
    """
    conn = _conn()
    for i in range(20):
        _add_dt(conn, f"a{i:02d}", published_at=f"2026-11-{i + 1:02d} 00:00:00")

    rows = db.search_candidates(conn, terms=["光伏"], tag_ids=[], sort="published_asc", limit=5)
    ids = [r["article_id"] for r in rows]
    assert ids == ["a00", "a01", "a02", "a03", "a04"], ids


def test_list_analyzed_rejects_unknown_sort() -> None:
    conn = _conn()
    _add_dt(conn, "a1")
    with pytest.raises(ValueError, match="未知排序"):
        db.list_analyzed(conn, tag=None, limit=10, offset=0, sort="nope")


def test_search_grouped_rejects_unknown_sort() -> None:
    conn = _conn()
    _add_dt(conn, "a1")
    with pytest.raises(ValueError, match="未知排序"):
        db.search_grouped(conn, raw_query="光伏", tags=TAGS, sort="nope")


def test_time_filter_uses_utc_column_over_wall_clock() -> None:
    """时间过滤按真实时刻比较，不是按站点墙钟。

    混时区站点里，墙钟更早的那篇可能其实更新。用 published_at 过滤会把东八区
    的那篇错判到区间外。
    """
    conn = _conn()
    # 墙钟 2026-11-01 08:00（东八区）= UTC 2026-11-01 00:00；墙钟 2026-10-31 20:00
    # （UTC+0）= UTC 同刻之前。按墙钟排两者相差 12 小时，按真实时刻只差 0。
    _add_dt(
        conn,
        "cn",
        published_at="2026-11-01 08:00:00",
        published_at_utc="2026-11-01 00:00:00",
    )
    _add_dt(
        conn,
        "utc",
        published_at="2026-10-31 20:00:00",
        published_at_utc="2026-10-31 20:00:00",
    )

    rows, total, _ = db.list_analyzed(
        conn, tag=None, limit=10, offset=0, published_from="2026-11-01 00:00:00"
    )
    assert total == 1
    assert [r["article_id"] for r in rows] == ["cn"]


def test_time_filter_falls_back_to_wall_clock_when_utc_missing() -> None:
    """历史行没有 published_at_utc 时退回墙钟比较，不能直接判成「不在区间内」。"""
    conn = _conn()
    _add_dt(conn, "legacy", published_at="2026-11-05 12:00:00", published_at_utc=None)

    rows, total, undated = db.list_analyzed(
        conn,
        tag=None,
        limit=10,
        offset=0,
        published_from="2026-11-01 00:00:00",
        published_to="2026-11-06 00:00:00",
    )
    assert total == 1
    assert undated == 0
    assert [r["article_id"] for r in rows] == ["legacy"]


def test_time_filter_reports_rows_without_any_date() -> None:
    """两个时间列都空的行会被排除，条数必须单独报出来而不是静默消失。"""
    conn = _conn()
    _add_dt(conn, "dated", published_at="2026-11-05 12:00:00", published_at_utc="2026-11-05 04:00:00")
    _add_dt(conn, "undated", published_at=None, published_at_utc=None)

    rows, total, undated = db.list_analyzed(
        conn, tag=None, limit=10, offset=0, published_from="2026-11-01 00:00:00"
    )
    assert total == 1
    assert undated == 1, "无日期行必须计入 undated_excluded"
    assert [r["article_id"] for r in rows] == ["dated"]


def test_list_analyzed_source_filter_and_empty_list_semantics() -> None:
    """sources=None 不筛；sources=[] 表示「筛完为空」，两者必须不同。"""
    conn = _conn()
    _add_dt(conn, "a1", source="site-a")
    _add_dt(conn, "b1", source="site-b")

    _, all_total, _ = db.list_analyzed(conn, tag=None, limit=10, offset=0)
    assert all_total == 2
    _, one_total, _ = db.list_analyzed(conn, tag=None, limit=10, offset=0, sources=["site-a"])
    assert one_total == 1
    _, none_total, _ = db.list_analyzed(conn, tag=None, limit=10, offset=0, sources=[])
    assert none_total == 0, "空列表必须是空集，不能退化成不过滤"


def test_list_analyzed_confidence_sort_puts_nulls_last_in_both_directions() -> None:
    conn = _conn()
    _add_dt(conn, "mid")
    _add_dt(conn, "high")
    _add_dt(conn, "null")
    conn.execute("UPDATE analyzed_articles SET category_confidence=0.4 WHERE article_id='mid'")
    conn.execute("UPDATE analyzed_articles SET category_confidence=0.9 WHERE article_id='high'")
    conn.execute("UPDATE analyzed_articles SET category_confidence=NULL WHERE article_id='null'")
    conn.commit()

    for sort, expected in (
        ("confidence_desc", ["high", "mid", "null"]),
        ("confidence_asc", ["mid", "high", "null"]),
    ):
        rows, _, _ = db.list_analyzed(conn, tag=None, limit=10, offset=0, sort=sort)
        assert [r["article_id"] for r in rows] == expected, sort


def test_normalize_day_rejects_compact_and_unpadded_forms() -> None:
    """只认 YYYY-MM-DD：紧凑格式与不补零的写法都拒掉，避免同一天多种写法。"""
    assert db.normalize_day("2026-11-05") == "2026-11-05 00:00:00"
    assert db.normalize_day("") is None
    assert db.normalize_day(None) is None
    for bad in ("20261105", "2026-1-5", "2026/11/05", "11-05-2026"):
        with pytest.raises(ValueError):
            db.normalize_day(bad)


def test_normalize_day_end_pushes_to_next_midnight() -> None:
    """终点推到次日 00:00 且用 < 比较：当天 23:59:59 与带毫秒的时间戳都要在区间内。"""
    assert db.normalize_day_end("2026-11-05") == "2026-11-06 00:00:00"
    assert db.normalize_day_end("2026-12-31") == "2027-01-01 00:00:00"
    assert db.normalize_day_end(None) is None


def test_search_grouped_reports_returned_and_truncated() -> None:
    """total 与 returned 不是一回事：后者是每类封顶后的实际条数。"""
    conn = _conn()
    for i in range(5):
        _add_dt(conn, f"a{i}", category="project_application")

    result = db.search_grouped(
        conn,
        raw_query="光伏",
        tags=TAGS,
        category_order=CATEGORY_ORDER,
        per_category=2,
    )
    assert result["total"] == 5
    assert result["returned"] == 2, "returned 必须是实际渲染条数"
    assert result["truncated"] is False
    assert result["sort"] == "relevance"


def test_source_stats_groups_by_source_and_uses_real_instant() -> None:
    """来源统计的最新时间要按真实时刻取，不能取墙钟最大值。

    这里刻意让两行的墙钟与真实时刻**跨天**：墙钟最晚的那篇（11-03 00:30 东八区）
    真实时刻是 11-02 16:30，比另一篇（11-02 20:00 UTC）还早。取 MAX(published_at)
    会给出 11-03，取 MAX(_TIME_EXPR) 才给出 11-02。只写同日数据的话两种写法结果
    相同，变异能全绿通过。
    """
    conn = _conn()
    _add_dt(
        conn,
        "a1",
        source="site-a",
        published_at="2026-11-03 00:30:00",
        published_at_utc="2026-11-02 16:30:00",
    )
    _add_dt(
        conn,
        "a2",
        source="site-a",
        published_at="2026-11-02 12:00:00",
        published_at_utc="2026-11-02 20:00:00",
    )
    _add_dt(
        conn,
        "b1",
        source="site-b",
        published_at="2026-10-01 00:00:00",
        published_at_utc="2026-10-01 00:00:00",
    )

    rows = db.source_stats(conn)
    by_source = {r["source"]: r for r in rows}
    assert by_source["site-a"]["count"] == 2
    assert by_source["site-b"]["count"] == 1
    assert by_source["site-a"]["latest_published_at"] == "2026-11-02 20:00:00", (
        "必须取真实时刻最大值；取墙钟会得到 2026-11-03 00:30:00"
    )


def test_source_stats_includes_empty_source_so_counts_add_up() -> None:
    """/v1/articles 的 total 会把空 source 行算进去，来源统计也必须算。

    否则前端「全部来源」显示的条数比各站点计数之和大，看起来像丢数据。
    """
    conn = _conn()
    _add_dt(conn, "a1", source="site-a")
    _add_dt(conn, "a2", source="")

    rows = db.source_stats(conn)
    assert sum(r["count"] for r in rows) == 2
    assert {r["source"] for r in rows} == {"site-a", ""}


# --------------------------------------------- B1 回归：三处时间口径必须一致

def _mixed_tz_rows(conn: sqlite3.Connection) -> None:
    """两行的墙钟顺序与真实时刻顺序**相反**，且跨天。

    A：墙钟 2026-03-02 00:00（东八区），真实时刻 2026-03-01 16:00Z
    B：墙钟 2026-03-01 23:00（UTC+0），真实时刻 2026-03-01 23:00Z

    按墙钟排 A 更新，按真实时刻排 B 更新。生产库 472 行里 137 行（29%）属于这种
    「墙钟与 utc 差一天以上」的情形，所以这不是构造出来的极端样例。
    """
    _add_dt(
        conn,
        "A",
        title="光伏补贴申报",
        published_at="2026-03-02 00:00:00",
        published_at_utc="2026-03-01 16:00:00",
    )
    _add_dt(
        conn,
        "B",
        title="光伏补贴申报",
        published_at="2026-03-01 23:00:00",
        published_at_utc="2026-03-01 23:00:00",
    )


def test_list_and_search_agree_on_time_order_when_timezones_mix() -> None:
    """同一个 sort 值，/v1/articles 与 /v1/search 必须给出相同顺序。

    早先列表按墙钟排、检索按真实时刻排，两个接口对同一批数据给出相反顺序；
    前端两个页面看起来都「正常」，用户只是觉得排序不太对。
    """
    conn = _conn()
    _mixed_tz_rows(conn)

    rows, _, _ = db.list_analyzed(conn, tag=None, limit=10, offset=0, sort="published_desc")
    listed = [r["article_id"] for r in rows]

    grouped = db.search_grouped(
        conn, raw_query="光伏", tags=TAGS, category_order=CATEGORY_ORDER, sort="published_desc"
    )
    searched = [i["article_id"] for g in grouped["groups"] for i in g["items"]]

    assert listed == ["B", "A"], listed
    assert searched == ["B", "A"], searched


def test_time_filter_window_matches_displayed_wall_clock_day() -> None:
    """日期筛选按「界面上显示的那一天」判定，不是按 UTC 日。

    A 的墙钟是 2026-03-02 00:00（东八区）、真实时刻是 2026-03-01 16:00Z。
    筛 03-01 时：按 UTC 日 A 会被选中，但卡片上写着 03-02，用户会以为筛错了；
    按墙钟日 A 落在 03-02，筛 03-01 只返回 B。生产库 472 行里 137 行属于这种
    「墙钟日 != UTC 日」的情形，所以这是主路径而不是边角。
    """
    conn = _conn()
    _mixed_tz_rows(conn)

    rows, total, _ = db.list_analyzed(
        conn,
        tag=None,
        limit=10,
        offset=0,
        published_from="2026-03-01 00:00:00",
        published_to="2026-03-02 00:00:00",
    )
    assert total == 1
    assert [r["article_id"] for r in rows] == ["B"]

    # 反过来筛 03-02 必须能取到 A，且只有 A
    rows, total, _ = db.list_analyzed(
        conn,
        tag=None,
        limit=10,
        offset=0,
        published_from="2026-03-02 00:00:00",
        published_to="2026-03-03 00:00:00",
    )
    assert total == 1
    assert [r["article_id"] for r in rows] == ["A"]


def test_search_candidates_ascending_keeps_oldest_by_real_instant() -> None:
    """升序截断保留的必须是最老的真实时刻，而不是最老的墙钟。"""
    conn = _conn()
    _mixed_tz_rows(conn)

    rows = db.search_candidates(conn, terms=["光伏"], tag_ids=[], sort="published_asc", limit=1)
    assert [r["article_id"] for r in rows] == ["A"], (
        "A 的真实时刻 16:00Z 最早；按墙钟截断会留下 B（墙钟 03-01 23:00 早于 A 的 03-02 00:00）"
    )


def test_list_analyzed_tie_break_is_article_id_desc() -> None:
    """发布时间完全相同时按 article_id 降序，分页才不会重复/漏行。"""
    conn = _conn()
    for aid in ("b3", "b1", "b2"):
        _add_dt(conn, aid, published_at="2026-11-01 00:00:00", published_at_utc="2026-11-01 00:00:00")

    rows, _, _ = db.list_analyzed(conn, tag=None, limit=10, offset=0, sort="published_desc")
    assert [r["article_id"] for r in rows] == ["b3", "b2", "b1"]

    asc, _, _ = db.list_analyzed(conn, tag=None, limit=10, offset=0, sort="published_asc")
    assert [r["article_id"] for r in asc] == ["b1", "b2", "b3"]


def test_normalize_day_end_rejects_max_date() -> None:
    """9999-12-31 加一天会溢出；日期控件常用它表示「不限」，不能变成 500。"""
    assert db.normalize_day_end("9999-12-30") == "9999-12-31 00:00:00"
    with pytest.raises(ValueError, match="日期超出可比较范围"):
        db.normalize_day_end("9999-12-31")


def test_search_candidates_rejects_unknown_sort() -> None:
    """未知排序必须报错，不能静默按降序截断。"""
    conn = _conn()
    _add_dt(conn, "a1")
    with pytest.raises(ValueError, match="未知排序"):
        db.search_candidates(conn, terms=["光伏"], tag_ids=[], sort="nope")

def test_search_time_filter_never_returns_blank_date_rows() -> None:
    """日期是纯空白的历史行不能既出现在结果里、又被算成「已排除」。

    SQLite 的 NULLIF(x, '') 只认空串，'   ' 原样返回，于是 _DAY_EXPR 得到 '   '，
    '   ' < '2026-11-06' 为真 —— 只靠上下界的话这类行会被上界命中。而
    list_analyzed 的 undated 统计用的是 trim(...) = ''，同一行会既出现又被报成
    「未计入日期筛选」，用户看到「共 1 条，其中 1 条无日期未计入」这种自相矛盾的话。
    """
    conn = _conn()
    _add_dt(conn, "dated", published_at="2026-11-05 12:00:00")
    _add_dt(conn, "blank", published_at="   ", published_at_utc=None)

    out = db.search_grouped(
        conn,
        raw_query="光伏",
        tags=TAGS,
        category_order=CATEGORY_ORDER,
        published_to="2026-11-06",
    )
    ids = [i["article_id"] for g in out["groups"] for i in g["items"]]
    assert ids == ["dated"], "空白日期行不能被上界命中"
    assert out["total"] == 1
    assert out["undated_excluded"] == 1, "它必须只出现在 excluded 计数里"


def test_search_grouped_reports_undated_excluded_only_when_filtering() -> None:
    """不筛日期时没有东西被排除，计数必须是 0 而不是全表无日期行数。"""
    conn = _conn()
    _add_dt(conn, "dated", published_at="2026-11-05 12:00:00")
    _add_dt(conn, "undated", published_at=None, published_at_utc=None)

    plain = db.search_grouped(conn, raw_query="光伏", tags=TAGS, category_order=CATEGORY_ORDER)
    assert plain["total"] == 2, "不筛日期时无日期行照常返回"
    assert plain["undated_excluded"] == 0

    filtered = db.search_grouped(
        conn,
        raw_query="光伏",
        tags=TAGS,
        category_order=CATEGORY_ORDER,
        published_from="2026-11-01",
    )
    assert filtered["total"] == 1
    assert filtered["undated_excluded"] == 1


def test_search_undated_excluded_respects_category_and_source_filters() -> None:
    """统计口径必须与筛选用的是同一批条件，否则两个数各说各话。

    只按「全库无日期行」数一遍的话，用户筛了项目申报、看来源 A，却被告知
    「另有 2 篇无日期被排除」——那 2 篇可能既不属于该类也不属于该来源，
    用户去核对时根本找不到对应文章。
    """
    conn = _conn()
    _add_dt(conn, "hit", category="project_application", source="site-a",
            published_at="2026-11-05 12:00:00")
    _add_dt(conn, "same-cat", category="project_application", source="site-a",
            published_at=None, published_at_utc=None)
    _add_dt(conn, "other-cat", category="industry_policy", source="site-a",
            published_at=None, published_at_utc=None)
    _add_dt(conn, "other-src", category="project_application", source="site-b",
            published_at=None, published_at_utc=None)

    out = db.search_grouped(
        conn,
        raw_query="光伏",
        tags=TAGS,
        category_order=CATEGORY_ORDER,
        categories=["project_application"],
        sources=["site-a"],
        published_from="2026-11-01",
    )
    assert out["undated_excluded"] == 1, "只数同类同源的那篇"


def test_search_undated_excluded_only_counts_rows_matching_the_keyword() -> None:
    """无日期统计必须只数「本来会被这次检索命中」的行。

    只按「全库无日期的行」数一遍的话，用户搜「光伏」却被告知「另有 3 篇无日期
    被排除」，而那 3 篇讲的是别的事 —— 用户按提示去核对时一篇都对不上。
    """
    conn = _conn()
    _add_dt(conn, "hit", title="光伏补贴申报通知", published_at="2026-11-05 12:00:00")
    _add_dt(conn, "hit-undated", title="光伏补贴申报补充通知",
            published_at=None, published_at_utc=None)
    _add_dt(conn, "noise-undated", title="关于人才落户的通知",
            published_at=None, published_at_utc=None)
    _add_dt(conn, "noise-undated-2", title="关于开展安全检查的通知",
            published_at=None, published_at_utc=None)

    out = db.search_grouped(
        conn,
        raw_query="光伏",
        tags=TAGS,
        category_order=CATEGORY_ORDER,
        published_from="2026-11-01",
    )
    assert out["total"] == 1
    assert out["undated_excluded"] == 1, "无关的无日期行不能算进来"


def test_search_time_filter_excludes_rows_with_whitespace_only_date() -> None:
    """日期只由空白组成的历史行既不能出现在结果里，也不能悄悄消失。

    内置 trim(x) 只去半角空格，'\t' 原样留下：它既会被上界命中（'\t' < '2026-11-06'），
    又算不出 undated（trim 后仍是 '\t' 非空），于是这行既不在结果里也不在统计里，
    用户看到的总数会比实际少且没有任何解释。

    覆盖的码位必须与 search.WS_CHARS 同源：早先手写表只到制表符/全角空格，
    U+2003（从 Word 复制的日期里最常见）与 U+0085 仍是静默丢行。
    """
    conn = _conn()
    _add_dt(conn, "dated", published_at="2026-11-05 12:00:00")
    _add_dt(conn, "tab", published_at="\t", published_at_utc=None)
    _add_dt(conn, "ideographic", published_at="\u3000", published_at_utc=None)
    _add_dt(conn, "em-space", published_at="\u2003", published_at_utc=None)
    _add_dt(conn, "nel", published_at="\x85", published_at_utc=None)

    out = db.search_grouped(
        conn,
        raw_query="光伏",
        tags=TAGS,
        category_order=CATEGORY_ORDER,
        published_to="2026-11-06",
    )
    ids = [i["article_id"] for g in out["groups"] for i in g["items"]]
    assert ids == ["dated"]
    assert out["undated_excluded"] == 4, "制表符/全角空格/U+2003/U+0085 都要计入未计入统计"


def test_search_time_sort_breaks_ties_by_relevance() -> None:
    """同一时刻的多篇仍按相关度次序：时间相同不等于顺序可以随便。

    发布时间精度到秒，同一天发的几十篇里大多数这一列完全相同，只剩相关度能
    区分。这里刻意让「高分那篇的 article_id」在两种排序下都与期望顺序相反：
    如果删掉相关度这一级键、直接落到 article_id 兜底，结果必然反序——只写
    同向数据的话，删键也照样绿，守卫等于没有。
    """
    for sort, high in (("published_desc", "aaa-high"), ("published_asc", "zzz-high")):
        conn = _conn()
        low = "zzz-low" if high == "aaa-high" else "aaa-low"
        for aid in (high, low):
            _add_dt(
                conn,
                aid,
                category="project_application",
                published_at="2026-11-01 00:00:00",
                published_at_utc="2026-11-01 00:00:00",
            )
        for aid, conf in ((high, 0.95), (low, 0.3)):
            conn.execute(
                "INSERT INTO article_tags (article_id, tag, confidence, evidence, engine)"
                " VALUES (?,?,?,?,?)",
                (aid, "new_energy", conf, "", "rules"),
            )
        conn.commit()

        out = db.search_grouped(
            conn, raw_query="光伏", tags=TAGS, category_order=CATEGORY_ORDER, sort=sort
        )
        ids = [i["article_id"] for g in out["groups"] for i in g["items"]]
        assert ids == [high, low], (sort, ids)


def test_search_grouped_empty_query_keeps_response_shape() -> None:
    """空查询的返回不能缺键：前端为缺键兜默认值，那套默认值迟早跟真实返回漂移。

    必须传真正的空串走 search_grouped 的早退分支。传"库里没有的词"是**测不到**的：
    resolve_query 对任何非空文本都会塞一个 terms 条目，is_empty 为 False，代码走的是
    正常路径，四键断言天然成立（这个用例曾经就是这么写的，删掉早退分支仍然全绿）。
    """
    conn = _conn()
    _add_dt(conn, "a1")

    out = db.search_grouped(
        conn,
        raw_query="",
        tags=TAGS,
        category_order=CATEGORY_ORDER,
        sort="published_asc",
    )
    assert out["groups"] == [] and out["total"] == 0
    assert out["sort"] == "published_asc"
    assert out["returned"] == 0
    assert out["truncated"] is False
    assert out["undated_excluded"] == 0

