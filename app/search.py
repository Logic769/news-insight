"""Keyword search over analyzed articles, grouped by first-level category.

Why this module exists
----------------------
The frontend takes a free-text keyword ("光伏", "专精特新") and wants five
categories as the outer grouping, with articles inside each category ranked by
how relevant they are to that keyword. All of that ordering has to be computed
here, because the frontend only receives rows -- it cannot rank by a signal it
never sees.

Two facts shape the design:

1. The tag vocabulary is closed (tags.json). A user keyword either *is* a tag
   id, *is* one of a tag's canonical keywords, or is an arbitrary string the
   model never saw. The first two map onto article_tags.confidence -- the
   model's own judgement about how strongly the article belongs to that
   concept, in [0, 1]. That is a far better relevance signal than term
   frequency, so it dominates the score.
2. The third case (arbitrary words, the long tail) has no tag row at all. It
   still has to be searchable, otherwise a user typing a brand-new phrase gets
   an empty page. For that we fall back to literal occurrence counts in
   title/body.

Relevance is therefore a blend, and the blend is deliberately explainable: every
hit reports which components fired, so a surprising ordering can be debugged
without re-running the model.

The score is bounded to [0, 1] by construction (each component is bounded and
the weights sum to 1.0), which keeps "confidence 0.9" and "relevance 0.9"
comparable instead of being two unrelated numbers that happen to look alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.tagging import TagDef

# 权重之和必须为 1.0：相关度与置信度同为 [0,1]，下游才能把两个数放在一起看。
# 标签置信度占七成，是因为它是模型对「这篇文章属于这个概念」的判断，比数词频可靠；
# 标题命中占两成，是因为它几乎没有噪声；正文命中只占一成，避免长文靠堆词赢过短文。
W_TAG = 0.70
W_TITLE = 0.20
W_BODY = 0.10

# 正文命中按出现次数饱和计数，不是线性：一篇长文把关键词重复 50 次并不比重复 5 次
# 更相关，线性计数只会让长文系统性占优。10 次即视为满分。
BODY_HIT_SATURATION = 10

# 反向包含（候选词出现在用户关键词内部）要求候选至少有 2 个字：单字候选在中文里
# 几乎必然误命中（"的"、"和"），把它们当检索词只会制造噪声。
MIN_CONTAINS_LEN = 2


@dataclass(frozen=True)
class Query:
    """A parsed user keyword: closed-set tag matches, plus weighted literal terms.

    `tag_matches` pairs each tag with how much of the user's query the matched
    surface form covered (0..1]. That number is what stops a generic fragment
    from outweighing a precise phrase -- see `resolve_query`.
    """

    text: str
    tag_matches: tuple[tuple[str, float], ...]
    terms: tuple[tuple[str, float], ...]

    @property
    def tag_ids(self) -> tuple[str, ...]:
        return tuple(t for t, _ in self.tag_matches)

    @property
    def term_texts(self) -> tuple[str, ...]:
        return tuple(t for t, _ in self.terms)

    @property
    def is_empty(self) -> bool:
        return not self.tag_matches and not self.terms


@dataclass
class Relevance:
    """Score for one article, with the components kept for debuggability."""

    score: float = 0.0
    tag_confidence: float = 0.0
    title_hits: int = 0
    body_hits: int = 0
    matched_tags: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "tag_confidence": round(self.tag_confidence, 4),
            "title_hits": self.title_hits,
            "body_hits": self.body_hits,
            "matched_tags": list(self.matched_tags),
            "matched_terms": list(self.matched_terms),
        }


# 空白字符表：SQL 侧（search_candidates 的 instr 预筛）与 Python 侧必须用同一张表，
# 否则两层判断会不一致 —— 搜「gpt 模型」时 SQL 拿带空格的原文去 instr，标题里写的是
# 「GPT模型」，候选集直接为空，用户看到 200 + 空结果，而打分逻辑其实能命中。
# 覆盖 Unicode 里会被当成分隔符的空白：ASCII 控制空白、NBSP、EM SPACE 一族、
# 行/段分隔符、IDEOGRAPHIC SPACE。从 Word/网页复制的关键词常带 \u2002/\u2003/\u2009，
# 表里漏一个就表现为「看起来一样的两个词搜不到」，且不报错。
WS_CHARS = (
    " ",
    "\t",
    "\n",
    "\r",
    "\v",
    "\f",
    "\x85",
    "\xa0",
    *(chr(cp) for cp in range(0x2000, 0x200B)),
    "\u2028",
    "\u2029",
    "\u3000",
)
_WS_TABLE = str.maketrans("", "", "".join(WS_CHARS))


def _norm(text: str) -> str:
    """Casefold + drop whitespace so 'AI', 'ai' and 'A I' are the same query."""
    return (text or "").translate(_WS_TABLE).casefold()


def resolve_query(raw: str, tags: list[TagDef]) -> Query:
    """Map a free-text keyword onto the closed vocabulary, keeping the raw term.

    Matching is generous on the way *in* (a tag matches when the query is its id,
    its Chinese name, or contains one of its keywords) and strict on the way
    *out* (only known tag ids are ever produced). Without the generous half,
    typing "光伏补贴" would match nothing even though it is plainly two known
    concepts; without the strict half the query would start inventing tags that
    article_tags can never contain.

    Every match also carries a coverage weight -- the share of the user's query
    the matched surface form covers, so a 2-character fragment inside a
    4-character query weighs 0.5 while an exact match weighs 1.0. This is a
    length penalty, and it is the whole mechanism: it does NOT try to guess
    which of two equally-covering fragments is more meaningful. For "人才申报"
    both talent ("人才") and project-application ("申报") cover half the query
    and get the same weight -- what separates the two kinds of article is then
    the model's tag confidence and the title hit, not this function.

    A tag may match through several of its surface forms at once ("人才" and
    "人才计划" for the same tag); the highest coverage wins, so typing the full
    label name always scores at least as well as typing a prefix of it.
    """
    text = (raw or "").strip()
    if not text:
        return Query("", (), ())

    needle = _norm(text)
    if not needle:
        return Query("", (), ())
    total_len = len(needle)

    terms: dict[str, float] = {text: 1.0}
    best_tag_weight: dict[str, float] = {}
    tag_order: list[str] = []

    for tag in tags:
        # 必须看完全部候选再定权重，不能命中第一个就 break：词表里大量短词是长词
        # 的前缀（「人才」→「人才计划」、「申报」→「申报指南」），先命中的若是短
        # 前缀，用户明明输入了完整标签词，覆盖度却只有一半。那样同一句话的排序还
        # 会随 keywords 的排列顺序变化，属于隐性耦合。
        for candidate in (tag.id, tag.name, *tag.keywords):
            cand = _norm(candidate)
            if not cand:
                continue
            if not (cand == needle or (len(cand) >= MIN_CONTAINS_LEN and cand in needle)):
                continue
            # 覆盖度：完整匹配记满分，子片段按长度占比折算。
            weight = 1.0 if cand == needle else min(1.0, len(cand) / total_len)
            if tag.id not in best_tag_weight:
                best_tag_weight[tag.id] = weight
                tag_order.append(tag.id)
            elif weight > best_tag_weight[tag.id]:
                best_tag_weight[tag.id] = weight
            # 短语里的已知词也单独作为文本词参与命中计数，让「光伏补贴」这种
            # 组合词在正文里也能被数到；但它只按覆盖度加权，不能顶替整词。
            prev = terms.get(candidate, 0.0)
            terms[candidate] = max(prev, weight)

    return Query(
        text,
        tuple((t, round(best_tag_weight[t], 4)) for t in tag_order),
        tuple((t, round(w, 4)) for t, w in sorted(terms.items(), key=lambda kv: -kv[1])),
    )


def _masked_hits(text: str, terms: tuple[tuple[str, float], ...]) -> tuple[int, float, list[str]]:
    """Count each occurrence of `text` once, longest surface form first.

    The vocabulary contains many short prefixes of longer labels (「人才」⊂「人才申报」).
    Counting every form independently would report three hits for a single occurrence
    and inflate the body component on short articles. Recording which positions were
    already consumed keeps the total at one hit per real occurrence while still letting a
    short form match text the long form does not cover. Positions are marked in a bytearray
    rather than by masking the match with a sentinel character: a sentinel only works if the
    text and the query never contain it, which is not something this function can assume.

    Returns the hit count, the summed coverage weight, and the forms that actually
    contributed. The third value keeps `matched_terms` honest: a short form swallowed by
    a longer one is *not* reported, otherwise the diagnostic list would claim a term
    contributed when the count says it did not.
    """
    # 用「已消费位置」而不是替换成占位符：占位符方案要额外假设「文本与查询都不含
    # 那个哨兵字符」（用 \x00 时，正文里真有 NUL 就会伪造命中），按位置记账没有这个
    # 前提。同一处文本只会被计一次，长形态（先遍历）吃掉的范围短形态也不再重复计。
    #
    # 记账用 bytearray 而不是区间列表：列表方案每命中一次就要跟全部已记区间比一遍，
    # 6000 字正文里单字高频命中时会退化到 O(n²)（实测 0.87s）。标记数组让每次检查只
    # 花 O(needle 长度)。
    consumed = bytearray(len(text))
    hits = 0
    # weight 是覆盖度乘子：每个形态只按其覆盖度累加一次，不随出现次数放大。
    # 评分侧是 min(1, hits) × min(1, weight)，覆盖度本就不该跟频次相乘。
    weight = 0.0
    used: list[str] = []
    for term, w in terms:
        needle = _norm(term)
        if not needle:
            continue
        size = len(needle)
        count = 0
        start = text.find(needle)
        while start != -1:
            end = start + size
            if not any(consumed[start:end]):
                consumed[start:end] = b"\x01" * size
                count += 1
            # 步进 +1 而不是 +size：这样能找出重叠起点（'aa' in 'aaa'），再由上面的
            # 标记数组把它们当成同一处文本只计一次。改成 +size 行为等价、只是少扫一遍，
            # 但会让「重叠只计一次」这层保证从「由标记数组拒绝」变成「根本不产生」，
            # 删掉标记检查时就不再有用例能抓住了。
            start = text.find(needle, start + 1)
        if not count:
            continue
        hits += count
        weight += w
        used.append(term)
    return hits, weight, used


def score_article(
    query: Query,
    *,
    title: str,
    body: str | None,
    tag_confidences: dict[str, float],
) -> Relevance:
    """Blend tag confidence with literal hit counts into one [0, 1] score.

    An article with no signal at all scores 0.0; the caller drops those rather
    than ranking them last -- "no relation" is not "weak relation".
    """
    rel = Relevance()
    title_n = _norm(title)
    body_n = _norm(body or "")

    if query.tag_matches:
        hits: list[tuple[str, float]] = []
        for tag_id, weight in query.tag_matches:
            # 夹到 [0,1]：库里历史行若存了 >1 的置信度（早期数据或手改过库），
            # 不夹的话 score 会突破 1.0，打破「总分落在 [0,1]」的契约。
            conf = min(1.0, max(0.0, float(tag_confidences.get(tag_id, 0.0))))
            if conf <= 0:
                continue
            # 覆盖度直接乘在置信度上：只有一半相关的泛词，最多只值一半置信度。
            hits.append((tag_id, conf * weight))
        if hits:
            # 多个标签命中时取最高的那个而不是求和：求和会让「宽泛词同时命中三个
            # 弱标签」压过「精准命中一个强标签」，与用户直觉相反。
            rel.tag_confidence = max(c for _, c in hits)
            rel.matched_tags = [t for t, _ in sorted(hits, key=lambda x: -x[1])]

    # 标题与正文按覆盖度加权求和：整词命中值为 1，子片段按长度折算，
    # 因此搜「人才申报」时，正文里只有「申报」的文章拿不到满分。
    #
    # 同一处文本只能被计一次：词表里短词常是长词的前缀（「人才」⊂「人才申报」），
    # 逐个形态独立 count 会让标题里一次「人才申报」被记成 3 次命中，正文项被系统性
    # 高估（越短的正文越明显）。所以按权重降序（长形态优先）逐个计数，命中后把该片段
    # 从待搜文本里抹成等长占位符 —— 等长是为了不打乱后续形态之间的相邻关系。
    title_hits, title_weight, title_used = _masked_hits(title_n, query.terms)
    body_hits, body_weight, body_used = _masked_hits(body_n, query.terms)
    rel.title_hits = title_hits
    rel.body_hits = body_hits
    # 按 terms 顺序合并，便于把 matched_terms 与 query.terms 对照着看。
    used = set(title_used) | set(body_used)
    rel.matched_terms = [term for term, _w in query.terms if term in used]

    # 标题命中一次即满分；正文按饱和次数折算，再乘覆盖度。
    title_ratio = min(1.0, float(rel.title_hits) / 1.0) * min(1.0, title_weight)
    body_ratio = min(1.0, float(rel.body_hits) / BODY_HIT_SATURATION) * min(1.0, body_weight)
    rel.score = W_TAG * rel.tag_confidence + W_TITLE * title_ratio + W_BODY * body_ratio
    return rel


def has_signal(rel: Relevance) -> bool:
    """True when anything matched; score alone can be 0 for a 0.0-confidence tag."""
    return bool(rel.matched_tags or rel.matched_terms)
