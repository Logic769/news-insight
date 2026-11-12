"""一级分类（单选）与「类内闭集打标」的回归用例。

覆盖四类曾经容易出错的边界：
- 模型返回大类词表外的 id（必须回落兜底，不能原样落库）；
- 兜底类只用规则引擎打 domain 粗标签，不做语义打标；
- 类内白名单以外的标签必须打不进来（跨类污染）；
- 未带 categories.json 的旧部署要退回单级打标，不能整体崩掉。
"""

from __future__ import annotations

import json

from app.tagging import (
    FALLBACK_CATEGORY,
    CategoryDef,
    CategoryHit,
    TagDef,
    category_with_llm,
    category_with_rules,
    load_categories,
    tag_with_rules,
    tags_for_category,
    tags_for_unclassified,
)


class FakeLLM:
    available = True

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, system, user, **kwargs):
        self.calls += 1
        return self.payload


CATEGORIES = [
    CategoryDef(
        "project_application",
        "项目申报",
        "申报通知",
        ("申报", "征集"),
        ("project_application", "planning"),
    ),
    CategoryDef("talent_application", "人才申报", "人才项目", ("人才计划", "职称"), ("hr_talent",)),
    CategoryDef("funding_subsidy", "资金补贴", "补贴奖补", ("补贴", "奖补"), ("subsidy", "funding")),
    CategoryDef(FALLBACK_CATEGORY, "其他", "", (), ()),
]

TAGS = [
    TagDef("project_application", "项目申报", "event", ("申报", "征集")),
    TagDef("planning", "规划计划", "policy", ("规划", "行动计划")),
    TagDef("hr_talent", "人才就业", "domain", ("人才", "职称")),
    TagDef("talent_application", "人才申报", "event", ("人才计划", "人才认定")),
    TagDef("subsidy", "补贴奖励", "policy", ("补贴", "奖补")),
    TagDef("funding", "资金支持", "policy", ("资助", "拨付")),
    TagDef("finance", "金融", "domain", ("银行", "利率")),
]


# --------------------------------------------------------------- 词表加载
def test_shipped_categories_are_closed_and_consistent():
    """仓库自带的 categories.json 与 tags.json 必须互相对得上，否则运行期才会炸。"""
    from pathlib import Path

    vocab_dir = Path(__file__).resolve().parent.parent / "app" / "vocab"
    payload, cats = load_categories(vocab_dir)
    assert payload["version"] >= 1
    assert [c.id for c in cats][-1] == FALLBACK_CATEGORY, "兜底类必须在末尾，便于人工核对"
    assert len({c.id for c in cats}) == len(cats)

    tag_ids = {t["id"] for t in json.loads((vocab_dir / "tags.json").read_text(encoding="utf-8"))["tags"]}
    for c in cats:
        assert c.id.strip() == c.id and c.id, f"大类 id 不能带空白: {c.id!r}"
        missing = [t for t in c.tags if t not in tag_ids]
        assert missing == [], f"大类 {c.id} 引用了不存在的标签: {missing}"
    assert cats[-1].tags == (), "兜底类不打二级标签，tags 必须为空"

    # 反向校验：非 domain 标签只能由大类白名单授权，没有类引用它就永远打不上。
    # domain 组是兜底类 other 的行业维度，走 tags_for_unclassified()，不在此约束内。
    referenced = {t for c in cats for t in c.tags}
    orphans = sorted(
        t["id"] for t in json.loads((vocab_dir / "tags.json").read_text(encoding="utf-8"))["tags"]
        if t["group"] != "domain" and t["id"] not in referenced
    )
    # 这 9 个是分类体系上线前就有的通用新闻事件标签（招标/IPO/财报/反腐…），
    # 与 5 大类正交，且用户明确要求 other 只保留 domain 组，因此当前确实打不上。
    # 列成显式名单是为了让「新增一个没人引用的标签」立刻变红；
    # 要清理这批遗留标签需要单独决策，不在分类改动的范围内。
    KNOWN_UNREACHABLE = {
        "anti_corruption", "disaster", "enterprise_dynamic", "financial_report",
        "investment", "ipo", "product_launch", "safety", "tender",
    }
    assert set(orphans) <= KNOWN_UNREACHABLE, (
        f"新增的业务标签必须被某个大类的 tags 引用，否则永远打不上: {sorted(set(orphans) - KNOWN_UNREACHABLE)}"
    )


def test_missing_fallback_category_warns(tmp_path, caplog):
    """词表缺兜底类时，表外结果无处可落，至少要能在日志里看出来。"""
    (tmp_path / "categories.json").write_text(
        '{"version": 1, "categories": [{"id": "a", "name": "甲", "keywords": [], "tags": []}]}',
        encoding="utf-8",
    )
    load_categories.cache_clear()
    try:
        payload, cats = load_categories(tmp_path)
        assert [c.id for c in cats] == ["a"], "缺兜底类不阻止加载，只是记一条告警"
        assert any("other" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    finally:
        load_categories.cache_clear()


def test_missing_categories_file_means_single_stage_tagging(tmp_path):
    """没带 categories.json 的旧部署要原样退回单级打标，而不是启动失败。"""
    (tmp_path / "tags.json").write_text('{"version": 1, "tags": []}', encoding="utf-8")
    payload, cats = load_categories(tmp_path)
    assert (payload, cats) == ({}, [])


# --------------------------------------------------------------- 一级分类
def test_out_of_vocab_category_falls_back():
    llm = FakeLLM({"category": "finance_policy", "confidence": 0.9, "evidence": "银行"})
    hit = category_with_llm(llm, CATEGORIES, "标题", "正文", body_chars=100)
    assert hit.category_id == FALLBACK_CATEGORY
    assert hit.confidence == 0.0, "词表外结果不能带着原置信度落库"


def test_empty_category_answer_falls_back_without_noise(caplog):
    llm = FakeLLM({"category": "", "confidence": 0.5})
    hit = category_with_llm(llm, CATEGORIES, "标题", "正文", body_chars=100)
    assert hit.category_id == FALLBACK_CATEGORY
    assert not [r for r in caplog.records if r.levelname == "WARNING"], "空答案不是词表外，不该告警"


def test_category_confidence_is_clamped():
    llm = FakeLLM({"category": "talent_application", "confidence": 7, "evidence": "人才计划"})
    assert category_with_llm(llm, CATEGORIES, "标题", "正文", body_chars=100).confidence == 1.0


def test_rules_category_picks_highest_score_single_label():
    """单选：命中词更多的类胜出，跨类也只返回一个。"""
    hit = category_with_rules(
        CATEGORIES, "关于申报人才计划与职称评审的通知", "本项目给予补贴", body_chars=200
    )
    # 人才类命中「人才计划」「职称」两词（1.2），申报类只命中「申报」（0.6）
    assert hit.category_id == "talent_application"
    assert hit.engine == "rules"
    assert hit.confidence <= 0.95


def test_rules_category_ties_keep_declaration_order():
    """并列时的确定性契约：按 categories.json 的声明顺序取第一个。

    这条不是「正确答案」，而是让同一篇文章在任何机器上归到同一类；
    调整类别顺序等于调整并列时的归属，属于行为变更。
    """
    hit = category_with_rules(
        CATEGORIES, "关于申报人才计划的通知", "", body_chars=200
    )
    # 「申报」(project_application) 与「人才计划」(talent_application) 各 0.6，先声明者胜
    assert hit.category_id == "project_application"
    assert hit.confidence == 0.6


def test_rules_category_high_score_is_not_overridden_by_clamped_rival():
    """高分先命中不能被后来的低分翻盘。

    confidence 有 0.95 封顶，若拿封顶值当比较阈值，后面任何一个原始分过 0.95
    的类都能覆盖前面的更高分，结果就取决于声明顺序了。
    """
    cats = [
        CategoryDef("a", "甲", "", ("甲一", "甲二"), ()),
        CategoryDef("b", "乙", "", ("乙一", "乙二", "乙三"), ()),
    ]
    # 甲在标题命中两词 = 1.2（封顶 0.95）；乙标题命中一词 + 正文命中两词 = 1.1
    hit = category_with_rules(cats, "甲一甲二 乙一", "乙二 乙三", body_chars=200)
    assert hit.category_id == "a"
    assert hit.confidence == 0.95, "封顶只作用于对外置信度，不影响类别选择"


def test_rules_category_never_returns_other_by_keyword():
    """兜底类的 keywords 是空的；即便有人给它配了词，也不能靠关键词命中 other。"""
    cats = [*CATEGORIES[:3], CategoryDef(FALLBACK_CATEGORY, "其他", "", ("其他",), ())]
    hit = category_with_rules(cats, "其他", "", body_chars=100)
    assert hit.category_id == FALLBACK_CATEGORY
    assert hit.confidence == 0.0


def test_rules_category_without_any_hit_is_other():
    hit = category_with_rules(CATEGORIES, "央行下调存款准备金率", "释放长期资金", body_chars=100)
    assert hit.category_id == FALLBACK_CATEGORY
    assert hit.evidence == ""


# --------------------------------------------------------------- 二级范围
def test_tag_scope_is_limited_to_category_whitelist():
    """跨类污染防线：资金补贴类拿不到人才/申报标签。"""
    scope = tags_for_category(CATEGORIES, TAGS, "funding_subsidy")
    assert {t.id for t in scope} == {"subsidy", "funding"}
    assert "project_application" not in {t.id for t in scope}


def test_unknown_category_has_empty_scope():
    assert tags_for_category(CATEGORIES, TAGS, "nonexistent") == []


def test_fallback_category_has_no_llm_scope():
    """other 的白名单是空的——线上靠它短路掉第二次模型调用。"""
    assert tags_for_category(CATEGORIES, TAGS, FALLBACK_CATEGORY) == []


def test_unclassified_scope_keeps_only_domain_group():
    scope = tags_for_unclassified(TAGS)
    assert {t.id for t in scope} == {"hr_talent", "finance"}, "只保留行业维度，业务标签一律不进"
    assert "subsidy" not in {t.id for t in scope}


def test_unclassified_rules_tagging_recovers_industry_tag():
    """央行降准这类不属于 5 大类的新闻，仍要靠规则引擎保住 finance。"""
    hits = tag_with_rules(
        tags_for_unclassified(TAGS),
        "央行下调存款准备金率",
        "银行体系流动性合理充裕，利率下行",
        max_tags=5,
        body_chars=200,
    )
    assert [h.tag_id for h in hits] == ["finance"]
    assert hits[0].engine == "rules"


def test_unclassified_rules_tagging_finds_nothing_returns_empty():
    hits = tag_with_rules(
        tags_for_unclassified(TAGS), "某地举办文化活动", "现场观众踊跃", max_tags=5, body_chars=200
    )
    assert hits == []


def test_category_hit_dataclass_defaults():
    """engine 取值是落库契约的一部分，默认值不能被顺手改掉。"""
    hit = CategoryHit("project_application", 0.8, "申报", "llm")
    assert (hit.category_id, hit.engine) == ("project_application", "llm")

# --------------------------------------------------------------- 落库保留语义
def test_save_analysis_keeps_category_when_not_provided(tmp_path):
    """没做分类的写入不能把库里已有的分类清掉。

    真实路径：部署回滚到旧版本、categories.json 被临时移走后退回单级打标、
    手工运维脚本重跑 —— 这些调用方都不会传 category。若 UPSERT 用
    excluded.category 全列覆盖，分类会被静默清空，只能 analyze --force 恢复。
    """
    from app import db

    path = tmp_path / "keep.db"
    db.init_db(path)
    conn = db.connect(path)
    kwargs = dict(
        article_id="a1", source="s", url="u", title="t",
        published_at=None, published_at_utc=None, content_length=0,
        tag_engine="", extract_engine="", tags=[], extractions=[],
    )
    try:
        db.save_analysis(conn, **kwargs, category="project_application",
                         category_confidence=0.9, category_engine="llm")
        assert tuple(conn.execute(
            "SELECT category, category_confidence, category_engine FROM analyzed_articles"
        ).fetchone()) == ("project_application", 0.9, "llm")

        db.save_analysis(conn, **kwargs)  # 不传分类 = 这次没做分类
        assert tuple(conn.execute(
            "SELECT category, category_confidence, category_engine FROM analyzed_articles"
        ).fetchone()) == ("project_application", 0.9, "llm"), "缺省 category 不能覆盖已有分类"

        # 明确判成 other 是真实结果，必须写进去（区别于「没提供」）
        db.save_analysis(conn, **kwargs, category=FALLBACK_CATEGORY,
                         category_confidence=0.0, category_engine="rules")
        assert tuple(conn.execute(
            "SELECT category, category_confidence, category_engine FROM analyzed_articles"
        ).fetchone()) == (FALLBACK_CATEGORY, 0.0, "rules"), "other 是有效分类，不能被当缺省值忽略"
    finally:
        conn.close()


# --------------------------------------------------------------- 老库迁移
OLD_SCHEMA = """
CREATE TABLE analyzed_articles (
    article_id       TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    published_at     TEXT,
    published_at_utc TEXT,
    content_length   INTEGER NOT NULL DEFAULT 0,
    tag_engine       TEXT,
    extract_engine   TEXT,
    analyzed_at      TEXT NOT NULL
);
"""


def test_init_db_migrates_old_database_without_category_columns(tmp_path):
    """已有 insight.db 升级是本改动最大的运行风险，必须有守护。

    用分类功能上线前的 DDL 建库并放一行历史数据，init_db() 之后要求：
    补齐三列、建出 idx_analyzed_category（它依赖新列，不能放在 SCHEMA_SQL）、
    历史行 category 保持 NULL（不等于 other）、重复调用幂等。
    """
    import sqlite3

    from app import db

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.execute(
        "INSERT INTO analyzed_articles (article_id, source, url, title, analyzed_at)"
        " VALUES ('legacy', 's', 'u', 't', '2026-01-01 00:00:00')"
    )
    raw.commit()
    raw.close()

    db.init_db(path)
    conn = db.connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(analyzed_articles)")}
        assert {"category", "category_confidence", "category_engine"} <= cols
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_analyzed_category'"
        ).fetchone() is not None, "依赖新列的索引必须在补列之后建出来"
        row = conn.execute(
            "SELECT category, category_confidence, category_engine FROM analyzed_articles WHERE article_id='legacy'"
        ).fetchone()
        assert tuple(row) == (None, None, None), "历史行是 NULL（分类上线前），不能补成 other"
    finally:
        conn.close()

    db.init_db(path)  # 幂等：老库已是新结构时再跑一次不能报错
    conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analyzed_articles").fetchone()[0] == 1
    finally:
        conn.close()

def test_single_stage_rerun_keeps_category_but_widens_tags(tmp_path):
    """categories.json 缺失时的降级重跑：分类保留，标签退回全词表。

    这是「移走词表文件后 force 重跑」这一运维动作的已知取舍，
    不是理想状态：可能出现 category='project_application' 却带类外标签的行。
    用例把它固定住，避免将来误以为 COALESCE 能同时保证标签也在类内；
    恢复 categories.json 后再跑一次即回到类内一致状态。
    """
    from app import db

    path = tmp_path / "degraded.db"
    db.init_db(path)
    conn = db.connect(path)
    kwargs = dict(
        article_id="a1", source="s", url="u", title="t",
        published_at=None, published_at_utc=None, content_length=0,
        tag_engine="", extract_engine="", extractions=[],
    )
    try:
        db.save_analysis(
            conn, **kwargs, tags=[("project_application", 0.9, "申报", "llm")],
            category="project_application", category_confidence=0.9, category_engine="llm",
        )
        # 降级：不带 category，tags 是全词表结果，含类外的 ipo
        db.save_analysis(
            conn, **kwargs, tags=[("ipo", 0.8, "上市", "llm")],
        )
        assert conn.execute(
            "SELECT category FROM analyzed_articles WHERE article_id='a1'"
        ).fetchone()[0] == "project_application", "降级重跑不改变已有分类"
        assert {r[0] for r in conn.execute(
            "SELECT tag FROM article_tags WHERE article_id='a1'"
        )} == {"ipo"}, "标签被整体替换成全词表结果（已知取舍）"
    finally:
        conn.close()
