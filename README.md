# news-insight

把 [news-crawler](../news) 抓到的新闻，变成**结构化、可查询的情报**：

1. **分大类** —— 每篇新闻先归入唯一的一个一级大类（项目申报 / 行业政策 / 人才申报 / 企业资质申报 / 资金补贴 / 其他）；
2. **打标签** —— 再**只在该大类允许的标签**里做闭集多标签打标，模型不许自己造标签；
3. **抽关键线索** —— 命中 `project_application` 这类标签后，进入 schema 驱动的事实抽取，例如项目申报的申报主体、材料清单、**截止时间**、资助额度、申报渠道；
4. **出准备清单** —— 把抽到的事实按申报执行顺序排成可直接照做的清单（确认项目 → 核对资格 → 备材料 → 系统填报 → 截止提交），每条标注原文依据或「流程建议」；
5. **落库 + 出接口** —— 结果存独立 SQLite，通过 HTTP API 查询，供后续业务系统消费。

它不抓网页、不解析正文、不碰爬虫的数据库。与 news-crawler 之间只有一条 HTTP 契约。

```
news-crawler (GPL-3.0)                    news-insight (MIT)
┌──────────────────────┐   GET /v1/articles   ┌────────────────────────────┐
│ GNE 抽取 + SQLite     │ ───────────────────► │ 一级分类（单选 6 类）        │
│ FastAPI /v1          │   GET /v1/articles/{id}│   └─► tag（类内闭集多标签） │
│ /app/data/news.db    │                      │        └─► 标签命中 schema？ │
│                      │                      │             └─► extract     │
└──────────────────────┘                      │ → insight.db（独立文件）     │
                                              │ → HTTP API :8020            │
                                              └────────────────────────────┘
```

## 为什么不写成爬虫项目里的一个模块

| 关注点 | news-crawler | news-insight |
| --- | --- | --- |
| 职责 | 抓取、正文抽取、去重 | 语义标注、结构化抽取 |
| 依赖 | lxml / Playwright | LLM API |
| 写库 | `news.db`（单 worker 写入） | `insight.db`（另一个进程，另一个文件） |
| 失败影响 | 抓不到新闻 | 分析结果缺失，可重跑 |
| 许可证 | GPL-3.0 | MIT |

两个进程写同一个 SQLite 文件会制造 `database is locked`，这也是本项目把结果放在独立库文件的原因。关联靠 `article_id`（上游 `id`）。

## 快速开始

```bash
cp .env.example .env        # 填 LLM_API_KEY；不填也能跑（降级为规则引擎）
docker compose up -d --build
curl http://localhost:8020/health

# 配了 INSIGHT_API_KEY 后，除 /health 与 /docs 外都要带 key
KEY=your-insight-key
curl -X POST "http://localhost:8020/v1/analyze?limit=10" -H "X-API-Key: $KEY"
# 长批次用异步作业：202 立即返回 job_id，轮询 /v1/jobs/{id}
curl -X POST "http://localhost:8020/v1/jobs" -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"limit":1000}'
curl "http://localhost:8020/v1/extractions?schema=project_application&due_before=2026-06-30" -H "X-API-Key: $KEY"
```

两个默认值值得先知道：

- **端口只绑 `127.0.0.1`**。`POST /v1/analyze` 会花 LLM 额度，不该默认暴露给局域网。要让别的机器访问，设 `INSIGHT_BIND=0.0.0.0`，**并先配好 `INSIGHT_API_KEY`**。
- **数据落在命名卷 `insight_data`**。bind mount 的 `./data` 由 docker daemon 以 `root:root` 创建，而容器以 uid 10001 运行，Linux 上首次启动会因 SQLite 无法读写而崩溃循环。想直接看 `insight.db`：`mkdir -p data && sudo chown 10001 data`，再设 `INSIGHT_DATA_DIR=./data`。

不启动爬虫也能验证：`NEWS_SOURCE_MODE=demo` 时使用 `fixtures/articles.json` 的内置新闻（含两篇申报公告、一篇降准、一篇融资）。

### 三种数据源模式

| 模式 | 行为 |
| --- | --- |
| `auto`（默认） | 先请求爬虫 `/v1`；连不上则回退内置演示数据，并在 `/v1/runs` 里记录实际模式 |
| `live` | 只走真实上游，失败直接报错（生产用） |
| `demo` | 完全不联网（CI / 演示用） |

### 接真实上游（示例：项目申报公告站点）

上游 [`news-crawler`](https://github.com/Logic769/news-crawler) 先按站点配置抓一批进 SQLite，再让本服务读它的 `/v1`。真实站点配置只放本地 `sources.local.json`（已被 `.gitignore` 忽略），不要提交：

```jsonc
// sources.local.json —— 站点配置（本地私有，不进 Git）
{
  "name": "example_notice",
  "list_url": "https://example.com/notice/",
  "item_xpath": "//table[contains(@class,'notice-list')]/tr",
  "title_xpath": ".//a/@title",
  "url_xpath": ".//a/@href",
  "date_xpath": ".//td[contains(@class,'date')]",
  "date_format": "%Y-%m-%d",
  "article_title_xpath": "//h1[contains(@class,'article-title')]/text()"
}
```

两个坑（一个已在代码里处理，一个靠配置约定）：

- 有些站点的 `<title>` 是站点名，真标题在正文 `h1` 里：配置 `article_title_xpath`，推荐以 `/text()` 结尾（写元素节点也能兜底，但不要用 `string()`，它只返回第一个字符）；
- 上游 `/v1/articles` 的 `order` 只接受 `published_desc` / `published_asc` / `updated_desc`，传别的会 422（本服务已固定用 `published_desc`）。

跑通后：

```bash
curl -s -X POST -H "X-API-Key: $INSIGHT_API_KEY" \
  "http://127.0.0.1:8020/v1/analyze?limit=10&force=true"
# {"mode":"live","scanned":10,"analyzed":10,"errors":0,"tag_engines":{"llm":10,"rules":0}}

curl -s -H "X-API-Key: $INSIGHT_API_KEY" "http://127.0.0.1:8020/v1/extractions?field=deadline"
# 申报截止日按区间右端取值：2025年11月25日17:00至2025年12月1日9:00 → 2025-12-01
```

### LLM 配置

任何 OpenAI 兼容接口都行：

```bash
# DeepSeek
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# 本地 Ollama（注意容器内不能写 127.0.0.1）
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen2.5:7b
LLM_API_KEY=ollama
```

**没有 LLM_API_KEY 时**：打标降级为关键词规则（`engine=rules`），申报抽取降级为正则 + 日期归一（`engine=rules`，置信度 0.4~0.5）。这不是替代方案，只是让流水线在没凭证时也能端到端跑通、便于验证部署；生产请配置模型。

### 吞吐与保留

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ANALYZE_CONCURRENCY` | 100 | 同时在途的文章数（1–200）；每篇仍串行 3 次 LLM 调用 |
| `ANALYZE_WRITE_BATCH` | 50 | 主线程每 N 篇提交一次 SQLite 事务 |
| `ANALYZE_MAX_PENDING_JOBS` | 50 | 异步作业排队上限（queued + running），超过 `POST /v1/jobs` 返回 429 |
| `NEWS_DETAIL_CONCURRENCY` | 16 | 从上游补正文的并发请求数（1–64） |
| `LLM_MAX_CONCURRENCY` | 100 | 供应商侧同时请求上限（1–200）；超过时本地排队，用于避免瞬时打爆网关 |
| `RETENTION_DAYS` | 90 | 分析结果保留天数（0–3650，0 = 不自动清理）；启动时和每次成功分析后执行硬删除 |

清理也可手动触发：

```bash
docker compose run --rm insight python -m app prune --dry-run   # 先看会删多少
docker compose run --rm insight python -m app prune --vacuum    # 删除并回收磁盘
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（无需鉴权，不回显内网地址） |
| POST | `/v1/analyze?limit=10&force=false` | 跑一批：拉取 → 打标 → 抽取 → 入库（同步兼容接口，`limit` 上限 50） |
| POST | `/v1/jobs` | 提交异步分析作业，202 返回 `{"data": {"id": ...}}`；body `{"limit":1000,"force":false,"concurrency":100}`，`limit` 上限 10000；排队超过 `ANALYZE_MAX_PENDING_JOBS` 返回 429 |
| GET | `/v1/jobs`、`/v1/jobs/{job_id}` | 作业列表与状态（`queued`/`running`/`succeeded`/`failed`，含结果统计） |
| GET | `/v1/articles?tag=project_application&limit=20` | 已分析文章列表，可按标签过滤；也可用 `category=funding_subsidy` 按大类过滤（可与 `tag` 同用，AND） |
| GET | `/v1/search?q=申报&category=project_application&per_category=20&include_body=false` | **关键词检索**：结果按 5 大类分组、类内按相关度降序（排序全部在后端算完，前端直接渲染；详见下节） |
| GET | `/v1/articles/{article_id}` | 单篇详情：标签 + 抽取结果 |
| GET | `/v1/articles/{article_id}/checklist` | 申报准备清单（按执行顺序排列，每项带 `basis=excerpt\|suggested` 与 `verified`）；`article_not_found` / `checklist_not_found` 两种 404 用错误码区分 |
| GET | `/v1/categories` | 一级分类词表（id/中文名/描述/关键词/允许的标签）+ 每类文章数 |
| GET | `/v1/categories/stats` | 分类分布与平均置信度（不含分类功能上线前的历史行） |
| GET | `/v1/tags` | 标签词表 + 每个标签的命中数；带 `category=` 时只统计该大类下的命中 |
| GET | `/v1/tags/stats` | 标签分布与平均置信度 |
| GET | `/v1/schemas` | 已注册的抽取 schema |
| GET | `/v1/extractions?schema=&field=&due_before=` | 抽取结果平铺查询，`due_before` 按截止日期过滤，可与 `field` 同时使用（按文章过滤，不是按字段） |
| GET | `/v1/runs` | 运行记录（模式、扫描/分析/跳过/错误数） |

CLI 也可：`docker compose run --rm insight python -m app analyze --limit 10 --concurrency 100`；清理历史数据：`python -m app prune --dry-run`、`python -m app prune --vacuum`。


### 关键词检索（后端算排序，前端只渲染）

搜索界面由前端实现，本服务只提供检索层与 API。**排序必须在后端算完**：前端虽然能从
`items[]` 里读到标签置信度，但拿不到查询覆盖度（那要复算词表匹配），默认也拿不到正文，
无法复算出同一个分数，所以 `groups[].items[]` 的顺序就是最终顺序。

```bash
curl -H "X-API-Key: $KEY" "http://localhost:8020/v1/search?q=%E4%BA%BA%E6%89%8D%E7%94%B3%E6%8A%A5&per_category=10"
```

```json
{
  "data": {
    "query": "人才申报",
    "matched_tags": ["talent_application", "project_application"],
    "terms": ["人才", "申报"],
    "total": 276,
    "groups": [
      {"category": "talent_application", "count": 20, "items": [
        {"article_id": "...", "title": "...", "published_at": "...", "tags": [...],
         "relevance": {"score": 0.765, "tag_confidence": 0.9, "title_hits": 1,
                       "body_hits": 3, "matched_tags": ["talent_application"],
                       "matched_terms": ["人才", "申报"]}}
      ]}
    ]
  }
}
```

**排序口径**：`score = 0.70×标签项 + 0.20×标题命中 + 0.10×正文命中`，三项都在 `[0,1]`，
所以总分天然落在 `[0,1]`，可直接当作前端的进度条或权重。

- 标签项 = 命中的闭集标签置信度 × 查询覆盖度（取最大值，不累加）。用户输入先映射到标签词表：
  输入「光伏」命中 `new_energy` 这个闭集标签，比字面匹配更贴近语义；
- 覆盖度 = 命中片段占用户输入的比例，实行**长度惩罚**：整词命中 1.0，4 字查询里命中 2 字记 0.5。
  一个标签可能同时命中多个候选形态（词表里大量短词是长词的前缀，如「人才」→「人才计划」），
  实现会遍历该标签全部候选再取最大值 —— 命中第一个就停会让用户输入完整标签名时只拿到 0.5，
  而且结果会随 `keywords` 的书写顺序变化。
  它**不做语义消歧** —— 「人才申报」里「人才」和「申报」各覆盖一半、权重相同，真正把两类文章分开的是标签置信度与标题命中。
  这一条有真实教训：最初以为覆盖度能区分这两类，实测才发现两类同为 0.5，`project_application` 曾凭置信度以 0.893 压过 `talent_application` 的 0.879。
- 标题命中按条数计（1 条即饱和），正文命中 10 条饱和 —— 长文堆词不再自动占优；
  计数按**出现次数**而不是「候选形态命中次数」：词表里短词常是长词前缀（「人才」⊂「人才申报」），
  若每个形态各自 `count`，标题里一次「人才申报」会被记成 3 次；实现按权重降序（长形态优先）逐个计数，
  命中过的位置记在一个 `bytearray` 里，后续形态只认同「未被占用的位置」的出现，保证一处文本只计一次，
  同时短形态仍能命中长形态覆盖不到的文本（不用哨兵字符占位，正文真含 NUL 也不会伪造命中）。
  同一形态的重叠出现（「aa」在「aaa」里）覆盖的是同一段字符，也只计一次。
  记账用标记数组而不是区间列表：列表方案每命中一次都要跟全部已记区间比一遍，
  6000 字正文里单字高频命中（「的」「年」）会退化到 O(n²)，实测 0.87s；标记数组同一场景 0.004s。
  `matched_terms` 只列实际贡献了命中的形态 —— 被长形态吃掉的短形态不列，否则排障时会误判它是加分来源；
- 分数相同按发布时间降序、再按 `article_id` 兜底，顺序不依赖 SQLite 的返回顺序（只按时间兜底时，同分同时间的几篇会退回行序，查询计划一变就抖）；
- 类顺序固定为 `categories.json` 的声明顺序，前端五段布局可以硬编码；
- 字面匹配用 `instr()` 而不是 `LIKE`：关键词是用户自由输入，`%` 和 `_` 在 `LIKE` 里是通配符，搜「50%」会命中全库。
  SQL 预筛与内存打分跑**同一份归一代码**：`search._norm` 被注册成 SQL 函数 `py_norm`，
  `instr(py_norm(title), ?)` 与打分侧的 `_norm()` 是同一个函数，物理上不会分叉。这不是洁癖 ——
  SQLite 的 `lower()` 只折 ASCII（`Ärger` 折完还是 `Ärger`），而 Python 的 `casefold()` 折成 `ärger`，
  两层用不同实现时「用户输入与标题字面完全一致」也会返回空结果（真实触发词：ÖBB、Ärzte、Müller、École）。
  空白同理：只对齐大小写不够，搜「gpt 模型」而标题写「GPT模型」时，SQL 拿带空格的原文去 `instr` 会直接空集。
  代价是每行每列每个形态一次 Python 回调。实测规模统一为「候选集打满 2000 行、正文 6000 字」
  （本机 min-of-3，不同机器可能差 2–3×）：

  | 场景（`instr(py_norm(title), ?) OR instr(py_norm(body), ?)`） | 预筛 | 全链路 |
  | --- | --- | --- |
  | **标题**命中（第一个条件即为真，短路掉 body） | 约 0.07s | 约 0.8–1.1s |
  | **仅正文**命中（标题不含，必须算完 body 才为真） | 约 0.68s | 约 1.3–1.7s |
  | 全不命中、1 个形态 | 约 0.66s | 约 1.1s（≥ 预筛；仅当 tags 也不命中时无候选可打分） |
  | 全不命中、3 个形态（每行算 3×2 列） | 约 2.0s | ≥ 预筛 |
  | 历史行 `body IS NULL`、全不命中 | 约 0.003s | 同上 |
  | 正文 2 万字、标题命中 | — | 约 3.6s |

  **只有「标题命中」才便宜**：`OR` 在首个条件为真时短路，省掉 `body` 的 6000 字归一。
  标题不含查询词时（真实新闻里用户查的行业词、细分词大多只在正文出现），`body` 的
  `py_norm` 每行都要付，成本与「全不命中」同档——**这是最常见路径，别按 0.07s 估**。
  纯 SQL `instr` 对照（不命中全扫）约 0.04s，即 `py_norm` 带来一个数量级预筛开销，
  这是「两侧归一必须同源」的代价。全链路的大头在逐行归一 + `score_article`（候选集打满时约 1s）。
  泛词会打满候选集，**超时请按秒级设**：6000 字正文约 4s，2 万字正文 + 多形态可达 8s（建议 10s），
  不要按「几百毫秒」估。

**为什么正文要落库**：只靠标题和标签，用户搜一个正文里才出现的词（如某个细分行业）会全军覆没。
正文在 `analyzed_articles.body`，只有上游详情页真取到文本时才覆盖（`COALESCE(NULLIF(excluded.body,''), body)`），
避免某次详情抖动把历史正文写成 `NULL` 让检索静默丢文章。检索默认不返回正文，`include_body=true` 才带。

历史行（本功能上线前分析的）`body` 为 `NULL`，需要回填一次（只搬文本、不重跑模型、不消耗 token）：

```bash
python -m app backfill-bodies --limit 500 --batch 50
```

回填只处理 `body IS NULL OR body = ''` 的行，可重复执行；单篇失败不中断整批，返回
`{"filled", "failed", "skipped", "remaining"}`，`remaining` 用来判断要不要再跑一轮。

**已知取舍**：

- 检索是字面 + 闭集标签的混合，没有分词也没有同义词扩展，「补贴」与「资助」靠词表里的 `keywords` 打通；
- 候选集上限 2000 行（`search_candidates(limit=2000)`），命中极多的泛词（如「的」）会被截断，所以 `total` 是**候选集内**命中的行数，不是全库真实命中数；
  候选集打满上限时服务端会记一条 WARNING（带 terms / tag_ids 数量），便于判断是不是关键词太泛导致结果不完整；
  判断方式是「多取一行看是否还有更多」，所以库里恰好 2000 行（全取完）不会误报；
- `category=` 不做闭集校验，未知类返回空结果而不是 422（同 `/v1/articles?category=`）；
- 分类功能上线前分析的历史行 `category` 为 `NULL`（≠ `other`），它们会单独成组，`category_order` 之外排在最后。

### 鉴权

设置 `INSIGHT_API_KEY` 后，除 `/health`、`/docs`、`/openapi.json` 外都要求 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`；`INSIGHT_REQUIRE_AUTH=1` 表示强制（未配置 key 时业务请求一律 401）。注意 `NEWS_API_KEY` 是**出站**凭证（访问爬虫），与本服务入站鉴权无关。

`/v1/analyze` 是同步兼容接口，`limit` 上限 50；分析作业**全局串行**，同步接口遇到已有批次会立刻返回 409，异步作业遇到分析锁被占用会直接标 `failed`（不会并行烧 token），排队作业总数超过 `ANALYZE_MAX_PENDING_JOBS` 时 `POST /v1/jobs` 返回 429。长批次请用 `POST /v1/jobs` + 轮询；单篇内部仍是「打标 → 抽取 → 清单」三次串行 LLM 调用，整体吞吐由 `ANALYZE_CONCURRENCY`（默认 100）决定。

## 扩展

### 加一级分类

编辑 `app/vocab/categories.json`，往 `categories` 里加一条：

```json
{
  "id": "funding_subsidy",
  "name": "资金补贴",
  "description": "财政资金、补贴、奖补、贴息、资助等资金支持政策与兑现通知。",
  "keywords": ["补贴", "补助", "奖补", "专项资金", "贴息"],
  "tags": ["subsidy", "funding", "policy_release"]
}
```

- 一级分类是**单选**：每篇只落一个 `category`，跨类内容取最贴近的一类；
- `tags` 是该类允许的二级标签白名单，**二级打标只能在这个集合里选**，这是防跨类污染的护栏（一篇人才通知不会被挂上资金补贴标签）；
- `other` 是兜底类：`keywords` 与 `tags` 留空。归到 `other` 的文章**不做语义打标**（省一次模型调用），只用规则引擎按 `group=domain` 保住行业标签（金融/科技/农业等），所以「央行降准」这类非 5 大类新闻仍能按 `finance` 查到；
- 并列时的归属由 `categories.json` 的**声明顺序**决定（先声明者胜），调整顺序等于调整并列归属，属于行为变更；比较用的是原始关键词分，不是封顶到 0.95 的 `confidence`，所以高分先命中的类不会被后面的低分翻盘；
- 大类与标签是**两级**关系：`/v1/articles?category=` 查大类，`?tag=` 查二级标签，两者可组合；
- `category` 参数不做闭集校验：传一个不存在的类名返回 200 空集（而不是 422），沿用标签参数的一贯行为；
  空串的语义在三个接口上**有意不同**，调用方不要混用：
  - `/v1/articles?category=`：空串按「未知类」处理（`a.category = ''`）→ 200 空集；
  - `/v1/search?category=`：空串归一为「不传」（`cleaned or None`）。若透传，SQL 会变成
    `COALESCE(category,'') IN ('')` 只命中「分类功能上线前」的历史行；若只在 Python 侧过滤掉空串，
    剩下的空列表又会被当成「筛完没结果」直接返回空集。两条路都会让用户只是清空了筛选框却看不到结果；
  - `/v1/tags?category=`：词表照旧返回，计数按该类统计（空串下计数全 0）；
- `categories.json` 里少了兜底类 `other` 时不会启动失败，但会记一条 WARNING：模型答出表外大类就没有去处，那篇文章的二级打标与抽取都会被跳过。

### 加标签

编辑 `app/vocab/tags.json`，往 `tags` 里加一条：`{ "id": "tender", "name": "招标采购", "group": "event", "keywords": ["招标","中标"] }`。

- `id` 是唯一契约，落库与查询都用它，**改名不要改 id**；
- `keywords` 只在无 LLM 的规则降级时使用，模型看的是 `id + name + group + keywords` 的完整描述；
- 几十个标签全部塞进单次 prompt 完全放得下，不需要两阶段召回。词表涨到几百条再考虑先粗筛候选；
- 新加的标签要在 `categories.json` 里被至少一个类引用，否则任何文章都打不到它（只有 `group=domain` 且文章落到 `other` 时才可能命中）；
- `group` 决定二级范围：`domain` 是行业维度，`event`/`policy` 是业务维度。归到 `other` 的文章只保留 `domain` 组，业务标签必须由大类白名单授权。

### 加抽取 schema

在 `app/vocab/schemas/` 下加 JSON：

```json
{
  "schema_id": "tender_notice",
  "name": "招标公告关键线索",
  "trigger_tags": ["tender"],
  "fields": [
    { "name": "tenderer", "type": "string", "description": "招标人" },
    { "name": "bid_deadline", "type": "date", "description": "投标截止时间" }
  ]
}
```

`trigger_tags` 就是路由：标签命中它才会跑这个抽取器，避免对无关新闻付 token。

## 设计决策

1. **先分大类，再在类内闭集打标。** 一级分类把标签空间切成互不重叠的几块：模型先选唯一一类，第二级只能看到该类允许的标签。好处是跨类污染从提示词层面就消失了（人才通知看不到资金补贴标签），代价是类别边界要人工维护；归到兜底类 `other` 的文章跳过第二次模型调用，只用规则引擎保住 `group=domain` 的行业标签，避免「不属于 5 大类」等于「一无所知」。
2. **闭集打标，标签外一律丢弃。** 放任模型自造标签，会把 `Artificial Intelligence` 和 `AI` 变成两个标签——这是 [karakeep](https://github.com/karakeep-app/karakeep) issue #111 记录的原始问题，#529 的解法就是「只允许使用用户已建的标签」，并已合入。本项目在 `tagging.py` 里做了同样的守卫（词表外丢弃 + 告警）。标签集合与「标签→schema 路由」借鉴 [autolabel](https://github.com/refuel-ai/autolabel) 的闭集思路。
3. **打标与抽取分离。** 打标的输出空间是闭合的（必须是词表内 id），抽取的输出空间是固定的（必须是 schema 内字段）；打标漏标只是少一条线索，抽取编造会污染下游。因此两者用的是不同 prompt、不同校验：标签必须 `in` 词表，抽取值必须能在原文定位。
4. **相对时间以发布时间为基准。** 「自发布之日起30日内」「本月底」如果用抓取时间换算，会随重跑静默漂移。抽取提示词里显式传入 `published_at_utc`（缺失时回退 `published_at`），`normalize_deadline()` 按下这个基准推算。
5. **缺失字段返回 null，值要能在原文定位。** `_locate()` 做空白无关的子串校验，定位不到的值保留但标 `verified=false`，供人工复核，而不是静默采信。模型给出的日期还会用原文重算一遍，与原文对不上就降级可信度；规则引擎（无 LLM 降级）产出的一切恒为 `verified=false`——正则只证明文本出现过，不证明语义正确（「3月1日至4月20日」取左端就是活生生的反例）。
6. **结果独立成库。** 见上文对照表。
7. **清单是派生视图，不是新事实。** 清单项要么引用已抽取的事实（`basis="excerpt"`：原文片段必须能在正文定位，且 `text` 里的日期/金额/电话等事实 token 必须被该片段支持，否则丢弃），要么显式标成 `suggested`（盖章、留存回执这类流程建议，禁止出现任何数字）。规则引擎生成的项沿用抽取行的 `verified` 标记（通常为 `false`），不会冒充已核验事实。它单独存在 `article_checklists`，重新生成不重写标签/抽取表；已分析但缺清单的文章会在下次运行自动补生成。
8. **单篇失败不拖垮批次，但「没拿到正文」不算成功。** 单篇异常计入 `errors` 并继续，运行统计落到 `runs` 表。详情接口没取到正文的文章只拿标题打标会把「确无截止日」永久定档，所以这类文章**不入库**、计入 `errors`，下次运行自然重试；`save_analysis` 整体在一个事务里，中途失败立即回滚，不会留下「有主行、无标签」的脏状态。

## 数据表

| 表 | 内容 |
| --- | --- |
| `analyzed_articles` | 已分析文章元数据（`article_id` 主键，与上游对齐），含一级分类 `category` 与 `category_confidence`/`category_engine`（`NULL` = 分类功能上线前分析的历史行，与 `other` 不是一回事）；`body` 存正文副本供检索（`NULL` = 落库时上游未提供，需 `backfill-bodies` 回填） |
| `article_tags` | `(article_id, tag)` 主键，含置信度、原文依据、引擎 |
| `extractions` | `(article_id, schema_id, field)` 主键，含值、原文片段、置信度、是否定位成功 |
| `article_checklists` | `article_id` 主键，清单项 JSON（`text`/`basis`/`source_text`）、生成引擎与时间 |
| `runs` | 每次批处理的统计 |
| `analysis_jobs` | 异步分析作业状态与结果（`queued`/`running`/`succeeded`/`failed`） |

## 测试与 CI

```bash
# 宿主机（dev 依赖已固定 ruff/mypy/pyyaml 版本，与 CI 一致）
pip install -e ".[dev]"
ruff check .
mypy
python tools/yaml_lint.py   # 与 CI 的 yaml lint job 跑同一份脚本
pytest -q

# 或在镜像里跑（tests/ 与 tools/ 不进镜像，必须挂载；镜像也不带 pytest）
docker compose run --rm -v "$(pwd)/tests:/app/tests" -v "$(pwd)/tools:/app/tools" \
  --entrypoint sh insight -c "pip install --user -q pytest pyyaml && python -m pytest tests -q"
```

`.github/workflows/ci.yml` 在 PR 与 master 推送时跑三个 job，全部为合并前必须通过的检查：

| job | 内容 |
| --- | --- |
| `python 3.10`–`3.14` | compileall + ruff + mypy（仅 3.12）+ pytest，五个版本矩阵 |
| `yaml lint` | 用 `tools/yaml_lint.py` 解析仓库内所有 YAML：拒绝重复映射键、支持多文档、不误杀 `<<` 合并键 |
| `docker build + container smoke` | 构建镜像、容器内跑 pytest、启动容器请求 `/health` |

配合仓库规则集 `pr-protection`（`master`）：禁止删除分支与强推、要求上述 7 个 GitHub Actions 检查全绿、要求所有 review thread 已解决、CodeRabbit 机器人为必过检查。`required_approving_review_count` 为 0——GitHub 不允许仓库所有者批准自己的 PR，若设为 1 会让单人维护的 PR 永久卡住。

规则集保留了 `bypass_actors`（RepositoryRole / always），单人维护时只有这一条路可走。允许 bypass 的情形仅限：**CodeRabbit 侧故障或配额耗尽导致 required check 永久 pending**，且此时必须人工确认本地 `ruff check .`、`mypy`、`pytest -q` 与 `docker build` 全部通过后再合入。GitHub Actions 的 7 个检查不允许 bypass——它们是本地可复现的。

覆盖：日期归一（绝对/相对/跨年/月底/无基准）、闭集守卫（词表外丢弃、去重、max_tags）、一级分类（词表外大类回落兜底、置信度夹取、并列按声明顺序、未带 `categories.json` 的旧部署退回单级打标、类别引用标签必须存在）、类内打标范围（跨类标签打不进、兜底类只保留 `domain` 组）、demo 模式端到端（分类 + 打标 + 申报抽取 + `due_before` 查询）、申报准备清单（规则顺序、`excerpt` 原文定位校验、「LLM 不可用 → 规则」降级、清单端点 404/200）、分类与标签端点、重跑幂等。

## 已知限制

- `q` 至少要有 1 个非空白字符：空串与纯空白都返回 422，避免同一件事两种响应码；

- 抽取质量取决于模型与 prompt，本项目只保证「不越界」（不造标签、不造字段、值可定位），不保证召回；
- 相对截止时间只覆盖常见表述（`N日内`/`N个工作日`/`本月底`），「公示结束后5个工作日」这类链式表述仍会漏；
- 分析作业在进程内全局串行（同步接口与异步 job 共用一把锁），长批次请用 `POST /v1/jobs`；单进程单 SQLite，不适合多副本水平扩展；
- 清单生成按篇多一次 LLM 调用（正文按 `EXTRACT_BODY_CHARS` 截断），每篇最多 4 次 LLM 请求（分大类 1 + 类内打标 1 + 抽取 1 + 清单 1；归到 other 时只有 1 次）；实际吞吐取决于供应商并发与限流，`ANALYZE_CONCURRENCY` 只是本地上限，`LLM_MAX_CONCURRENCY` 另外限制真正同时在途的供应商请求；遇到 429/5xx 会优先按 `Retry-After`、否则指数退避并加抖动重试；
- `RETENTION_DAYS` 默认 90：启动时和每次成功分析后自动硬删除更早的 `analyzed_articles`/`article_tags`/`extractions`/`article_checklists`、`runs` 以及已完成的 `analysis_jobs`，不保留归档；
- 一级分类的边界靠 `categories.json` 的 `description` 与 `keywords` 人工维护：规则引擎按关键词计分，遇到「专项资金申报通知」这种同时命中资金补贴与项目申报的文章，会归到分数更高的那一类，另一类的二级标签随之拿不到（目前 `funding_subsidy` 的白名单不含 `project_application`，这类文章不会触发申报抽取）；需要时给该类补 `project_application` 标签即可；
- 分类按「保留式」写入：重跑时没做分类（例如临时移走 `categories.json`、回退到旧版本）只会更新标签，不会清空已有分类；但此时标签是按全词表打的，可能出现分类与标签不同源的行（`category=project_application` 却带 `ipo` 标签）。恢复词表后再跑一次即回到类内一致；
- 分类功能上线前已分析的历史行 `category` 为 `NULL`：`/v1/categories` 里统计不到、`?category=` 也筛不出来，需要 `analyze --force` 重跑才会补上分类；这与归到 `other` 是不同的状态；
- 检索是字面匹配 + 闭集标签的混合，不做分词、不做同义词扩展（「补贴/资助」靠词表 `keywords` 打通），也不做拼音或错别字容忍；候选集上限 2000 行，命中面极广的泛词会被截断；
- 检索排序是**人工设定的权重**（标签 0.70 / 标题 0.20 / 正文 0.10），不是学习出来的排序模型；它保证「标签命中优先、长文不占优、结果稳定可复现」，不保证符合每个人的直觉。要调权重改 `app/search.py` 顶部常量并补用例；
- 正文副本（`body`）使库体积约增加一倍多，且会随保留期一起被删除；`RETENTION_DAYS` 之后连同正文一起消失，检索结果也随之减少；
- 演示数据与规则引擎只用于跑通链路，不作为效果基线。

## 致谢

架构取舍来自这几个项目的公开实践：[karakeep](https://github.com/karakeep-app/karakeep)（闭集标签）、[autolabel](https://github.com/refuel-ai/autolabel)（LLM 标注 + 校验）、[TrendRadar](https://github.com/sansan0/TrendRadar)（新闻流水线与推送形态）、[GeneralNewsExtractor](https://github.com/kingname/GeneralNewsExtractor)（上游正文抽取）。

## License

MIT，见 [LICENSE](LICENSE)。注意上游 news-crawler 为 GPL-3.0，本项目通过 HTTP 调用其 API，不链接其代码。
