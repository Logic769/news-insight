# 多 Agent 协作开发说明（Project Guide）

> 本文档面向参与本项目的所有开发者与自动化 Agent。开始修改代码前，请先完整阅读本文档；如果本文档与代码实现冲突，以代码为准，并同步更新本文档。
>
> 姊妹文档：上游爬虫项目 [news-crawler](https://github.com/Logic769/news-crawler) 的 `AGENTS.md`。两份文档结构相同，但职责边界严格分开：本项目**不抓取、不解析 HTML、不碰爬虫数据库**。

---

## 1. 项目概况

**news-insight** 是 news-crawler 的下游分析服务，把新闻爬虫抓到的新闻变成**结构化、可查询的情报**：

1. **分大类** —— 先单选一个一级分类（项目申报 / 行业政策 / 人才申报 / 企业资质申报 / 资金补贴 / 兜底 `other`），类别定义在 `categories.json`；
2. **打标签** —— 只能从该类允许的受控标签（闭集多标签）中选择，模型不许自造标签，也拿不到别的类的标签；
3. **抽关键线索** —— 标签命中 schema 的 `trigger_tags` 后，进入 schema 驱动的事实抽取（例如项目申报的申报主体、材料清单、截止时间、资助额度、申报渠道）；
4. **出准备清单** —— 把抽到的事实按申报执行顺序排成可直接照做的清单（确认项目 → 核对资格 → 备材料 → 系统填报 → 核对经费 → 记下咨询方式 → 截止前提交），每项标注原文依据或「流程建议」；
5. **落库 + 出接口** —— 结果存独立 SQLite，通过 FastAPI 的 `/v1` 暴露，供后续业务系统消费。

它与上游之间只有一条 HTTP 契约：读 `news-crawler` 的 `/v1/articles`（列表）与 `/v1/articles/{id}`（详情），用上游的 `id` 作为 `article_id` 对齐。

### 为什么独立成项目

| 关注点 | news-crawler | news-insight |
| --- | --- | --- |
| 职责 | 抓取、正文抽取、去重 | 语义标注、结构化抽取 |
| 依赖 | lxml / Playwright | LLM API |
| 写库 | `news.db`（单 worker 写入） | `insight.db`（另一个进程、另一个文件） |
| 失败影响 | 抓不到新闻 | 分析结果缺失，可重跑 |
| 许可证 | GPL-3.0 | MIT |

两个进程写同一个 SQLite 文件会制造 `database is locked`，这是结果放在独立库文件的直接原因；许可证隔离（GPL-3.0 与 MIT）也要求只通过 HTTP 调用、不链接上游代码。

### 关键信息

| 项目 | 内容 |
| --- | --- |
| 当前正式版本 | `0.1.0`（`pyproject.toml` 与 `app/__init__.py` 必须同步） |
| 开源协议 | MIT（全文见 `LICENSE`） |
| 主仓库 | https://github.com/Logic769/news-insight |
| 上游仓库 | https://github.com/Logic769/news-crawler（GPL-3.0，仅 HTTP 调用） |
| 默认分支 | `master` |
| Python 版本要求 | `>=3.10`（CI 矩阵覆盖 3.10–3.14） |
| 默认端口 | `8020`（容器内 uvicorn；宿主机默认只绑 `127.0.0.1`） |

---

## 2. 技术栈

| 类别 | 技术 | 说明 |
| --- | --- | --- |
| 语言 | Python 3.10+ | 使用 `from __future__ import annotations` 与联合类型语法 |
| HTTP API | FastAPI + Uvicorn | `create_app()` 工厂，`/v1` 路由，Pydantic v2 校验 |
| 出站 HTTP | httpx | 上游客户端与 LLM 客户端共用；LLM 客户端全进程共享连接池 |
| 数据存储 | SQLite（标准库 sqlite3） | 单文件 `insight.db`；WAL + `busy_timeout=30000` |
| 并发模型 | `concurrent.futures.ThreadPoolExecutor` | 文章级并发；**主线程唯一写者**，按批 commit |
| LLM | 任意 OpenAI 兼容 `/chat/completions` | `response_format={"type":"json_object"}`，返回 JSON |
| 打包 | hatchling + pyproject.toml | `pip install .`，可选 `[dev]` / `[yaml]` |
| 代码质量 | ruff + mypy | 规则与版本固定在 `pyproject.toml`，与 CI 一致 |
| 测试 | pytest | `tests/` 下 12 个测试文件 |
| CI | GitHub Actions | `ci.yml`：python 3.10–3.14、yaml lint、docker build + container smoke |
| 部署 | Docker / Docker Compose | 镜像 `news-insight:0.1.0`，容器内 uid 10001 |

运行依赖（`pyproject.toml`）：

```toml
"fastapi>=0.110"
"uvicorn[standard]>=0.27"
"httpx>=0.27"
"pydantic>=2.6"
```

开发依赖 `[dev]`：pytest、ruff（固定版本）、mypy（版本与 CI 一致）、pyyaml、types-PyYAML。`[yaml]` 只装 pyyaml —— 专供 yaml lint job，避免为校验两个 YAML 拉全部运行依赖。

---

## 3. 目录结构与职责

```text
news-insight/
├── app/
│   ├── __init__.py            # __version__ = "0.1.0"
│   ├── __main__.py            # CLI：analyze / prune / init / backfill-bodies / health
│   ├── settings.py            # frozen dataclass Settings，全部来自环境变量
│   ├── news_client.py         # 上游 /v1 只读客户端（分页 + 详情补正文）
│   ├── llm.py                 # OpenAI 兼容 chat_json（连接池 + 信号量 + 退避重试）
│   ├── tagging.py             # 一级分类（单选）+ 类内闭集多标签打标（LLM + 规则降级）
│   ├── extraction.py          # schema 驱动事实抽取（日期归一 + 原文定位校验）
│   ├── checklist.py           # 申报准备清单（派生视图，LLM + 规则降级）
│   ├── checklist_service.py   # 清单输入适配与生成服务（API 与 pipeline 共用）
│   ├── pipeline.py            # 单批次编排：拉取 → 并发分析 → 主线程批量落库
│   ├── jobs.py                # 进程内异步作业 JobManager + ANALYZE_LOCK
│   ├── prune.py               # 保留期清理（默认 90 天）
│   ├── search.py             # 关键词检索与相关度打分（前端拿不到查询覆盖度，排序必须后端算）
│   ├── backfill.py           # 给历史行回填正文副本（只搬文本，不重跑模型）
│   ├── db.py                  # SQLite schema 与全部读写函数
│   ├── main.py                # FastAPI 应用工厂与 /v1 路由
│   └── vocab/
│       ├── categories.json    # 一级分类词表（6 类：5 大类 + other 兜底）
│       ├── tags.json          # 受控标签词表（45 个标签，3 个分组）
│       └── schemas/
│           └── project_application.json   # 申报关键线索 schema（9 字段）
├── fixtures/articles.json     # demo 模式内置 4 篇新闻
├── tests/                     # pytest：test_*.py 共 13 个
├── tools/yaml_lint.py         # CI 的 YAML 门禁脚本（拒绝重复映射键；支持多文档；不误杀 << 合并键）
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── renovate.json
├── README.md                  # 面向用户的中文使用文档
├── AGENTS.md                  # 本文档，面向开发者与 Agent
├── LICENSE                    # MIT
└── .github/workflows/ci.yml
```

### 模块职责速查

| 函数 / 类 | 位置 | 职责 |
| --- | --- | --- |
| `Settings` / `load_settings()` | `app/settings.py` | 冻结配置对象，全部字段由环境变量注入；缺 `INSIGHT_API_KEY` 且未强制时打 warning |
| `NewsClient.list_articles()` | `app/news_client.py` | 读上游列表，分页直到取够 `limit`；返回 `(articles, mode)` |
| `NewsClient.hydrate_bodies()` | `app/news_client.py` | live 模式逐篇拉详情补正文，按 `NEWS_DETAIL_CONCURRENCY` 并发，去重，失败标 `body_ok=False` |
| `LLMClient.chat_json()` | `app/llm.py` | 调 `/chat/completions` 并解析 JSON；429/5xx 退避重试（优先 Retry-After） |
| `load_vocab()` | `app/tagging.py` | 读 `vocab/tags.json`，返回 (原始 payload, [TagDef])；`lru_cache` |
| `load_categories()` / `classify_article()` | `app/tagging.py` | 读 `vocab/categories.json`；单选一级分类，LLM 不可用时降级规则引擎，缺文件退回单级打标 |
| `tags_for_category()` / `tags_for_unclassified()` | `app/tagging.py` | 把二级标签收窄到大类白名单；兜底类只保留 `domain` 组 |
| `tag_article()` | `app/tagging.py` | 返回 `(hits, engine)`；有 LLM 走模型，失败或无凭证降级规则引擎 |
| `load_schemas()` / `extract_article()` | `app/extraction.py` | 按 `trigger_tags` 路由 schema；日期归一与原文定位校验 |
| `build_checklist()` | `app/checklist.py` | 由已抽取事实生成准备清单；LLM 失败降级规则引擎 |
| `checklist_input_from_detail()` / `build_for_input()` | `app/checklist_service.py` | 从文章详情构造清单输入（要求项目申报标签、抽取块与正文）；API 与 pipeline 共用的生成入口 |
| `analyze_batch()` | `app/pipeline.py` | 单批次全流程编排，返回本次统计 |
| `JobManager` | `app/jobs.py` | 异步作业提交/执行/状态落库；单 worker 串行 |
| `prune()` | `app/prune.py` | 删除超过保留期的分析结果，可选 VACUUM |
| `create_app()` | `app/main.py` | 构建 FastAPI 应用、鉴权中间件与 `/v1` 路由 |
| `resolve_query()` / `score_article()` | `app/search.py` | 把用户输入映射到闭集标签 + 字面词，并按「标签置信度 × 覆盖度 + 标题/正文命中」打分 |
| `search_grouped()` | `app/db.py` | 检索入口：候选集 → 打分 → 按 6 大类（含 `other` 兜底）分桶，类内按所选 `sort` 排序（默认相关度降序），返回前端直接渲染的结构 |
| `search_candidates()` | `app/db.py` | 候选集：`py_norm` 归一后的 `instr` 预筛，截断前按与 `sort` 一致的时间序（`published_asc` 最早优先，其余最新优先，`article_id` 兜底）定序后 `LIMIT`，保证截断集合确定 |
| `backfill_bodies()` | `app/backfill.py` | 给历史行补 `body`（逐篇取上游详情），可重复执行、单篇失败不中断 |
| `save_analysis()` | `app/db.py` | 幂等替换单篇分析结果（SAVEPOINT + 可选批量提交） |
| `prune_old_data()` | `app/db.py` | 子表 → 主表 → runs → 已完成 analysis_jobs 的硬删除 |

### 本地私有文件（绝对不要提交）

- `.env`：本地凭证（LLM key、`INSIGHT_API_KEY`、`NEWS_API_KEY`）；
- `data/`、`data_live/`、`*.db`、`*.db-wal`、`*.db-shm`：本地数据库与侧车文件；
- `backups/`：本地备份（可能含 `.env` 与站点配置）。

以上均已在 `.gitignore` 中；`.dockerignore` 另外排除 `.env`、`data`、`tests`、缓存目录。

---

## 4. 核心数据流

```text
docker compose up -d
        │
        ▼
POST /v1/jobs（异步）或 POST /v1/analyze（同步）或 CLI python -m app analyze
        │
        ▼
analyze_batch()
        ├── NewsClient.list_articles(limit)      # 上游 /v1/articles 分页，order=published_desc
        ├── NewsClient.hydrate_bodies()          # live 模式逐篇 GET /v1/articles/{id} 补 raw_content
        ├── db.analyzed_ids() 预过滤             # 已分析 → skipped（不论清单是否齐全）
        │      └── 已分析但缺清单 → 并发补生成
        ├── ThreadPoolExecutor(_analyze_one)     # 每篇：分大类 → 类内打标（other 走规则）→ 命中 schema 才抽取 → 申报清单
        ├── 主线程 _persist_result()             # SAVEPOINT 写单篇，每 ANALYZE_WRITE_BATCH 篇 commit
        └── db.record_run_finish()               # runs 统计（scanned/analyzed/skipped/errors/detail）
        │
        ▼
insight.db（analyzed_articles / article_tags / extractions / article_checklists / notifications / runs / analysis_jobs）
        │  analyzed_articles.body 随分析结果落库（检索用），历史行靠 backfill-bodies 回填
        │
        ▼
GET /v1/articles、/v1/articles/{id}、/v1/articles/{id}/checklist、/v1/extractions、/v1/categories、/v1/tags/stats、/v1/search
```

### 并发与写入模型（关键）

1. **文章级并发，篇内串行**：`ANALYZE_CONCURRENCY`（默认 100，上限 200）控制同时在途的文章数；每篇文章内部依次「分大类 → 类内打标 → 每个命中 schema 各一次抽取 →（命中申报时）清单」；当前只有 1 个 schema，因此最多 4 次 LLM 调用（归到 `other` 时只有 1 次），**不合并、不省略**——质量优先。
2. **worker 不碰 SQLite**：`_analyze_one()` 只做网络与计算，返回 `_AnalysisResult`；所有写库由主线程 `_persist_result()` 完成。SQLite 只有一个写者，这是避免 `database is locked` 的核心约定，改并发相关代码时不得破坏。
3. **批量提交**：`ANALYZE_WRITE_BATCH`（默认 50）篇提交一次；`save_analysis(commit=False)` 在没有外层事务时会显式 `BEGIN`，否则 SAVEPOINT 的 RELEASE 会立即提交、批量提交退化为逐篇提交。
4. **单篇失败隔离**：入库用 SAVEPOINT，一篇失败只回滚该篇并计入 `errors`，批次继续；批次级异常会 rollback 并写 `runs.finished_at`，不留永久「运行中」的孤儿行。
5. **空正文不入库**：详情接口没拿到正文（`body_ok=False` 或正文为空）的文章只拿标题打标会把「确无截止日」永久定档，因此这类文章不计入 `analyzed`，计入 `errors`，下次运行自然重试。
6. **幂等重跑**：`save_analysis()` 是「替换式」——先删本文章的 `article_tags`/`extractions` 再插入；已分析文章默认跳过，`force=true` 时重跑覆盖。

---

## 5. 对外 API

启动：`docker compose up -d`，默认 `http://127.0.0.1:8020`，交互文档 `/docs`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（无需鉴权，不回显内网地址） |
| GET | `/v1/categories` | 一级分类词表（id/中文名/描述/关键词/允许的标签）+ 每类文章数 |
| GET | `/v1/categories/stats` | 分类分布与平均置信度（不含 `category IS NULL` 的历史行） |
| GET | `/v1/sources` | 按抓取站点聚合：`source`（空串=未标注来源）、`source_filter`（筛选回传值，未标注来源为 `__unlabeled__`）、`count`、`avg_confidence`、`latest_published_at`（真实时刻） |
| GET | `/v1/tags` | 标签词表 + 每个标签命中数；带 `category=` 时只统计该类下的命中 |
| GET | `/v1/tags/stats` | 标签分布与平均置信度 |
| POST | `/v1/analyze?limit=10&force=false` | 同步跑一批（`limit` 上限 50）；已有分析或清单生成任务持锁时 409，失败 502 |
| POST | `/v1/jobs` | 提交异步分析作业，202 返回 job；body `{"limit":1000,"force":false,"concurrency":100}` |
| GET | `/v1/jobs`、`/v1/jobs/{job_id}` | 作业列表与状态（`queued`/`running`/`succeeded`/`failed`） |
| GET | `/v1/articles?tag=&category=&industry=&limit=&offset=` | 已分析文章列表，可按标签、大类和/或受控行业过滤（AND 组合，`limit` ≤ 200） |
| GET | `/v1/search?q=&category=&industry=&source=&published_from=&published_to=&sort=&per_category=&include_body=` | 关键词检索：按 6 大类分组、类内按所选 `sort` 排序（默认相关度降序；排序全部在后端算完，前端只渲染）；`q` 必填 1–100 字，`category`/`industry`/`source` 可重复也可逗号分隔，`sort` 取 `relevance\|published_desc\|published_asc`（见 `SEARCH_SORTS`；未知值 422 而非静默降序。`confidence_desc/asc` 只有 `/v1/articles` 列表支持），`per_category` 1–100 |
| GET | `/v1/industries` | 受控行业词表 + 每个行业文章数（`meta.total`/词表版本），供前端筛选与徽标 |
| GET | `/v1/articles/{article_id}` | 单篇详情：标签 + 抽取结果 |
| GET | `/v1/articles/{article_id}/checklist` | 申报准备清单；`article_not_found`/`checklist_not_found` 两种 404 用错误码区分 |
| POST | `/v1/articles/{article_id}/checklist` | 生成/复用申报清单，body `{"force": false}`：默认幂等，`force=true` 重新生成；404 `article_not_found` / 409 `checklist_not_applicable`（字符串）；已有分析/清单生成任务持锁时也 409，但 `error.code` 为数字 409 |
| GET | `/v1/notifications?limit=20&unread_only=false` | 站内消息提醒（新分析出的项目申报公告）；`meta.total` 与列表同筛选，`meta.unread` 为全库未读数，同一篇只提醒一次 |
| POST | `/v1/notifications/{notification_id}/read` | 标记单条已读；不存在 404 |
| POST | `/v1/notifications/read-all` | 全部标记已读，返回更新条数与 `unread: 0` |
| GET | `/v1/schemas` | 已注册的抽取 schema 列表 |
| GET | `/v1/extractions?schema=&field=&due_before=` | 抽取结果平铺查询 |
| GET | `/v1/runs` | 运行记录（模式、扫描/分析/跳过/错误数） |

### 关键语义

- **鉴权**：设置 `INSIGHT_API_KEY` 后，除 `/health`、`/docs`、`/redoc`、`/openapi.json` 外都要求 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`；`INSIGHT_REQUIRE_AUTH=1` 表示强制（未配置 key 时业务请求一律 401）。`NEWS_API_KEY` 是**出站**凭证（访问爬虫），与入站认证无关。
- **错误体**：422 校验错误为 `{"error": {"code": 422, "status": 422, "message", "details"}}`；`HTTPException`（404/409/502 等）只有 `{"error": {"code": <HTTP 码>, "message"}}`，**没有 `status` 与 `details`**，客户端以 HTTP 状态码为准。
- **同步 vs 异步**：`/v1/analyze` 是兼容接口，`limit` 上限 50；长批次用 `POST /v1/jobs`（`limit` 上限 10000）。两者共享 `ANALYZE_LOCK`：同一时刻只允许一个批次在跑，避免并行重复烧 token；job 拿不到锁直接标 `failed`（不排队等待）。
- **作业队列**：进程内 `ThreadPoolExecutor(max_workers=1)`；相同 payload 的未完成 job 复用同一 id；排队（queued + running）超过 `ANALYZE_MAX_PENDING_JOBS`（默认 50）返回 429 `too_many_jobs`；进程启动时把上次遗留的 `queued` 与 `running` job 一并标为 `failed`（进程内执行器重启后无法恢复）。
- **`due_before` 语义**：按文章级过滤——先找出 `deadline` 不晚于该日期的文章，再返回这些文章的抽取记录。**未指定 `field` 时只返回 `deadline` 行**（避免同一篇文章的 materials/contact 等行重复返回）；指定 `field` 时返回该字段行，但同样只限截止日期不晚于该日期的文章。
- **`/health`**：不回显 `news_base_url`，避免未鉴权端点泄漏内网地址。
- **`category` 与 `tag`**：`category` 是**单选**一级分类，`tag` 是该类白名单内的多标签；两个过滤条件 AND 组合。`category IS NULL` 表示分类功能上线前分析的历史行，`?category=other` 筛不到它们，需要 `analyze --force` 重跑。
- **`/v1/search` 的排序契约**：搜索界面由前端实现，本服务只提供检索层与 API。**排序必须在后端算完** —— 前端虽能从 `items[]` 读到标签置信度，但拿不到查询覆盖度（要复算词表匹配）、默认也拿不到正文，无法复算出同一个分数，所以 `groups[].items[]` 的顺序就是最终顺序，改动排序逻辑等于改动前端可见行为。
  - 口径：`score = 0.70×标签项 + 0.20×标题命中 + 0.10×正文命中`，三项均落在 `[0,1]`，总分天然有界（权重和 = 1.0，改权重时必须保持这个性质，否则 `score` 不再能直接当百分比用）；
  - 标签项 = 命中标签的 `confidence` × 查询覆盖度（取最大、不累加；`confidence` 先夹到 `[0,1]`，历史脏数据不能让总分突破上界）。覆盖度 = 命中片段占用户输入的长度比，整词 1.0、4 字查询里命中 2 字记 0.5；一个标签可能同时命中多个候选形态（「人才」和「人才计划」都命中「人才计划」），**必须遍历完该标签全部候选再取最大值** —— 命中第一个就停会让完整标签词只拿到前缀分（实测 0.5），结果还会随 `keywords` 排列顺序变化。它是**长度惩罚，不做语义消歧** —— 「人才申报」里 `talent_application`(人才) 与 `project_application`(申报) 同为 0.5，区分两类的实际是标签置信度与标题命中。**这条曾写错过**：最初 docstring 与用例断言前者 1.0，实测 `0.5 == 1.0` 断言失败，结论是代码对、说明错；
  - 标题命中 1 条饱和、正文命中 10 条饱和（`BODY_HIT_SATURATION`），避免长文堆词占优。命中数按**出现次数**统计，不是「候选形态命中次数」：`resolve_query("人才申报")` 的 `terms` 是 `(("人才申报",1.0),("人才",0.5),("申报",0.5))`，逐形态各自 `count` 会把标题里一处「人才申报」记成 3 次、系统性高估正文项（短文最明显）。`_masked_hits()` 按权重降序（长形态优先）逐个计数，命中过的位置记在 `bytearray` 里，后续形态只认同「未被占用的位置」的出现 —— 同一处文本只会被计一次（同一形态的重叠出现如「aa」在「aaa」里也只计一次），且不需要假设文本里不含某个哨兵字符（早期实现用 `\x00` 占位，正文真含 NUL 时会伪造命中）；记账用标记数组而非区间列表：列表方案每命中一次都要与全部已记区间比对，6000 字正文单字高频命中退化为 O(n²)（实测 0.87s vs 0.004s，`test_masked_hits_stays_fast_on_high_frequency_short_term` 卡 0.2s 上限防回归）；`weight` 是覆盖度乘子，每个形态只累加一次、不随出现次数放大（评分侧是 `min(1,hits) × min(1,weight)`，覆盖度本就不该乘频次）；短形态仍能命中长形态覆盖不到的文本（正文只写「请于本周内申报」时 `body_hits=1`、`matched_terms=['申报']`）。`_masked_hits` 同时返回「实际贡献了命中的形态」，`matched_terms` 只由它拼成 —— 被长形态吃掉的短形态不再列进去，否则排障时会以为该词贡献过分；
  - 分数相同按 `published_at` 降序、再按 `article_id` 兜底，顺序不依赖 SQLite 返回顺序（只按时间兜底时，同分同时间的几篇会退回 rowid 顺序，查询计划一变就抖）；
  - 类顺序固定为 `categories.json` 的声明顺序，声明外的分类（如历史行 `category IS NULL`）排在最后；
  - 字面匹配用 `instr()` 而非 `LIKE`：关键词是用户自由输入，`%`/`_` 在 `LIKE` 里是通配符，搜「50%」会命中全库；`category=` 同 `/v1/articles` 一样不做闭集校验，未知类返回 200 空集；空串（`?category=`）在 `/v1/search` 里显式归一成「不传」（`cleaned or None`）—— 透传会让 SQL 变成 `COALESCE(category,'') IN ('')` 只命中历史 NULL 行，而过滤后剩下的空列表又会被 `search_candidates` 当成「筛完没结果」返回空集；`/v1/articles` 用的是 `if category is not None` 判空串语义，两者有意不同；
  - 候选集上限 `_CANDIDATE_LIMIT = 2000`（`search_candidates` 的默认值），泛词会被截断，`total` 是**候选集内**命中的行数，不是全库真实命中数；截断前按与 `sort` 一致的真实时刻顺序取（`published_asc` 用 `_TIME_EXPR ASC, article_id ASC` 保留**最早**的 2000 行，其余排序用 `_TIME_EXPR DESC, article_id DESC` 保留**最新**的 2000 行），**这个顺序不是展示顺序**（`search_grouped` 之后会重排），它只决定「哪些行活过截断」—— 没有 `ORDER BY` 时同一查询两次可能截到不同集合，用户会看到结果忽有忽无；`search_grouped` 用 `limit=_CANDIDATE_LIMIT + 1` 多取一行判断「是否还有更多」，`len(rows) > _CANDIDATE_LIMIT` 才记 WARNING（带 terms / tag_ids 数量）并截断 —— 直接比对 `== limit` 会在库里恰好 2000 行（其实全取完了）时误报；
  - SQL 预筛与内存打分跑**同一份归一代码**：`_ensure_norm_func()` 把 `search._norm` 注册成 SQL 函数 `py_norm`（`deterministic=True`，每次查询前注册，幂等），预筛写成 `instr(py_norm(a.title), ?) > 0`，与打分侧是同一个函数。**不能各写一套**：SQLite 的 `lower()` 只折 ASCII（`Ärger` 折完还是 `Ärger`），Python 的 `casefold()` 折成 `ärger`，两层不一致时「用户输入与标题字面完全一致」也会返回空集（200 + 空结果，不报错；真实触发词 ÖBB / Ärzte / Müller / École）。空白同理，搜「gpt 模型」而标题写「GPT模型」；
  - 代价是每行每列每个形态一次 Python 回调，且**成本取决于是否「标题」命中**（本机 min-of-3，规模「候选集打满 2000 行 × 正文 6000 字」，不同机器差 2–3×）：标题命中预筛约 0.05s / 全链路约 0.5s（`OR` 首个条件为真，短路掉 body 的 6000 字归一）；**仅正文**命中约 0.55s / 约 2.0s（标题不含，必须算完 body 才为真，成本与不命中同档，且候选打满时每行都要打分）；全不命中约 0.52s / 约 0.5s（候选为空、不打分）；`body IS NULL` 的历史行约 0.003s（回填前仍能按标题/标签命中）。**只有标题命中才便宜，而用户查的行业词/细分词大多只在正文出现——这是最常见路径，别按 0.05s 估**。纯 SQL `instr`（不命中全扫）约 0.04s，即 `py_norm` 带来一个数量级预筛开销，这是「两侧归一必须同源」的代价。逐行归一 + `score_article` 是另一块大头（仅正文命中且候选打满时约 1.5s）。泛词会打满候选集，**超时按秒级设**（最坏约 2–3s，建议 10s）；若将来候选集需放大到十万级，再考虑 FTS5 或物化归一列；
  - `search.WS_CHARS` 覆盖 ASCII 控制空白、`\x85`、`\xa0`、`\u2000`–`\u200a`、`\u2028`、`\u2029`、`\u3000`（共 22 个）。从 Word / 网页复制的关键词常带 `\u2002`/`\u2003`/`\u2009`，漏一个就表现为「看起来一样的两个词搜不到」且不报错；
  - `q` 必须含至少 1 个非空白字符（`pattern=r"\S"`）：只靠 `min_length` 时 `q=""` 是 422 而 `q=" "` 会走到 200 空 groups，同一件事两种响应码；
- **正文副本 `analyzed_articles.body`**：检索需要正文里有而标题里没有的词，因此正文随分析结果一起落库。写入用 `COALESCE(NULLIF(excluded.body, ''), body)` —— 只有本次真拿到文本时才覆盖，避免某次详情抖动把历史正文写成 `NULL` 让检索静默丢文章。历史行 `body IS NULL` 用 `python -m app backfill-bodies` 回填（只搬文本、不重跑模型、可重复执行、单篇失败不中断整批）；`body_html` 存清洗后的正文 HTML（`html_sanitize.sanitize_body_html` 白名单），同一条回填命令一并处理，已有一侧不会被空值覆盖；这使库体积约增加一倍多，且随 `RETENTION_DAYS` 一起被删除。

### 6.1 受控行业 `app/vocab/industries.json`

十个行业（**闭集单选**）：`next_gen_it` 新一代信息技术、`high_end_equipment` 高端装备制造、`new_materials` 新材料、`biomedicine` 生物医药、`new_energy` 新能源、`energy_saving` 节能环保、`digital_creative` 数字创意、`modern_agriculture` 现代农业、`modern_services` 现代服务业、`other` 其他。

结构：`version` + `industries`（每条 `{id, name, description, keywords}`）；`id` 是持久化契约（落 `analyzed_articles.industry`），改名不改 id。分类走 `tagging.classify_industry`：LLM 可用先按 `INDUSTRY_SYSTEM_PROMPT` 选一个，`LLMError` 降级为关键词规则（标题命中 +1.0、正文 +0.5，最高分胜出）；表外 id 丢弃并回落到 `other`。历史行用 `python -m app backfill-industries` 回填，只写 `industry`/`industry_confidence`/`industry_engine` 三列，不重跑标签/抽取/清单。

---

## 6. 标签词表与抽取 schema

### 6.0 一级分类 `app/vocab/categories.json`

六个大类（**单选**）：`project_application` 项目申报、`industry_policy` 行业政策、`talent_application` 人才申报、`enterprise_certification` 企业资质申报、`funding_subsidy` 资金补贴、`other` 兜底。

结构：`version`、`description`、`categories`（每条 `{id, name, description, keywords, tags}`）。

- `tags` 是该类**允许的二级标签白名单**（引用 `tags.json` 的 id），二级打标只在这个交集里选；这是防跨类污染的护栏；
- `other` 的 `keywords` 与 `tags` 都为空：归到兜底类的文章**不调第二次 LLM**，只用规则引擎按 `OTHER_RULE_GROUPS = {"domain"}` 打行业标签（`tagging.py` 的 `tags_for_unclassified()`），所以「央行降准」这类非 5 大类新闻仍能按 `finance` 查到；
- 规则引擎单选：各类按关键词计分（标题 0.6 / 正文 0.25），**并列时按 `categories.json` 的声明顺序取第一个**——调整顺序等于调整并列归属，属行为变更；
- 文件缺失不是错误（`load_categories()` 返回 `({}, [])`）：pipeline 退回单级打标，未带 `categories.json` 的旧部署仍可启动；
- 新增大类后必须保证 `tags` 里的每个 id 都存在于 `tags.json`；反向也成立——新增的业务标签（`group` 非 `domain`）必须被某个大类引用，否则永远打不上。`tests/test_categories.py::test_shipped_categories_are_closed_and_consistent` 双向校验，其中 9 个分类体系上线前遗留的通用事件标签（招标/IPO/财报/反腐等）列在白名单里豁免；
- API 侧 `?category=` 不做闭集校验，未知类名返回 200 空集（与 `?tag=` 一致）。

### 6.1 标签词表 `app/vocab/tags.json`

结构：`version`、`description`、`groups`（domain / event / policy 三组）、`tags`（45 条，每条 `{id, name, group, keywords}`）。

`group` 的两层含义：`domain` 是行业维度（金融/科技/农业…），`event`/`policy` 是业务维度（申报/补贴/资质…）。归到 `other` 的文章只保留 `domain` 组，业务标签必须由大类白名单授权。

- `id` 是唯一契约：落库、`/v1/articles?tag=` 过滤、schema 的 `trigger_tags` 都用它，**改名不要改 id**；
- `keywords` 只在无 LLM 的规则降级时使用；模型看到的是 `id + name + group + keywords` 的完整描述；
- 几十个标签全部塞进单次 prompt 完全放得下，不需要两阶段召回；词表涨到几百条再考虑粗筛候选；
- 闭集守卫在 `tagging.py`：模型返回的 `id` 不在词表内直接丢弃并 `LOG.warning`，绝不入库。

### 6.2 抽取 schema `app/vocab/schemas/*.json`

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

- `trigger_tags` 就是路由：标签命中它才会跑这个抽取器，避免对无关新闻付 token；
- 字段 `type` 目前支持 `string` / `date` / `array`（`materials` 这类清单字段输出字符串数组）；
- `deadline` 的日期归一与区间取右端逻辑在 `extraction.py`，schema 描述里写的「原文为起止区间时取区间结束日期」是行为约定，改代码要同步改描述。

### 6.3 加分类 / 加标签 / 加 schema 的标准流程

1. 编辑 `app/vocab/categories.json`、`app/vocab/tags.json` 或新增 `app/vocab/schemas/<id>.json`；
2. **新标签必须被某个大类的 `tags` 引用**，否则没有文章能打到它（例外：`group=domain` 的标签在文章落到 `other` 时仍可能被规则引擎命中）；
3. 跑 `pytest`（词表与 schema 会被 demo 端到端用例加载，`tests/test_categories.py` 会校验类别引用的标签都存在）；
4. `python -m app analyze --limit 5`，检查 `/v1/categories/stats`、`/v1/tags/stats` 与 `/v1/extractions`；
5. 确认无回归后再提交；schema 变更要同步 README 与本文档。

---

## 7. 数据库设计

### 7.1 表结构

```sql
CREATE TABLE analyzed_articles (
    article_id       TEXT PRIMARY KEY,   -- 与上游 article id 对齐
    source           TEXT NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    published_at     TEXT,               -- 上游墙钟
    published_at_utc TEXT,               -- 真实时刻，相对日期换算基准
    content_length   INTEGER NOT NULL DEFAULT 0,
    body             TEXT,               -- 正文副本，供关键词检索；NULL = 落库时上游未提供（历史行，需 backfill-bodies）
    body_html        TEXT,               -- 清洗后的正文 HTML，详情页渲染用；同样可由 backfill-bodies 回填
    category         TEXT,               -- 一级分类 id；NULL = 分类上线前的历史行，与 'other' 不同
    category_confidence REAL,
    category_engine  TEXT,               -- llm / rules / none
    industry         TEXT,               -- 受控行业 id；NULL = 行业功能上线前的历史行
    industry_confidence REAL,
    industry_engine  TEXT,               -- llm / rules / none
    tag_engine       TEXT,               -- llm / rules
    extract_engine   TEXT,               -- llm / rules / none
    analyzed_at      TEXT NOT NULL
);

CREATE TABLE article_tags (
    article_id TEXT NOT NULL,
    tag        TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence   TEXT,
    engine     TEXT NOT NULL,            -- llm / rules
    PRIMARY KEY (article_id, tag)
);

CREATE TABLE extractions (
    article_id TEXT NOT NULL,
    schema_id  TEXT NOT NULL,
    field      TEXT NOT NULL,
    value_json TEXT NOT NULL,            -- JSON 文本（标量带引号、数组是 JSON）
    raw_text   TEXT,
    confidence REAL NOT NULL,
    verified   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (article_id, schema_id, field)
);

CREATE TABLE article_checklists (        -- 派生视图：重新生成不碰事实表
    article_id   TEXT PRIMARY KEY,
    engine       TEXT NOT NULL,
    items_json   TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE notifications (             -- 站内提醒：同一篇只提醒一次
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,           -- ISO8601 UTC 带 Z，前端直接 new Date()
    read_at     TEXT
);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,
    scanned     INTEGER NOT NULL DEFAULT 0,
    analyzed    INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    detail      TEXT
);

CREATE TABLE analysis_jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,           -- queued/running/succeeded/failed
    payload     TEXT NOT NULL,           -- {"limit":..,"force":..,"concurrency":..}
    result      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
```

索引：`idx_tags_tag(article_tags.tag)`、`idx_analysis_jobs_status(analysis_jobs.status, created_at)`、`idx_analyzed_published(analyzed_articles.published_at DESC)`、`idx_analyzed_category(analyzed_articles.category, published_at DESC)`、`idx_analyzed_recency(analyzed_articles(COALESCE(published_at, analyzed_at) DESC, article_id DESC))`、`idx_analyzed_recency_utc(_TIME_EXPR DESC, article_id DESC)`、`idx_analyzed_source_time(source, _TIME_EXPR DESC)`、`idx_analyzed_day(_DAY_EXPR DESC)`、`idx_analyzed_asc_undated_last((_TIME_EXPR IS NULL), _TIME_EXPR ASC, article_id ASC)`、`idx_extractions_schema(extractions.schema_id, field)`。

`idx_analyzed_category` 建在 `_POST_MIGRATION_SQL` 而不是 `SCHEMA_SQL`：`executescript` 会先于补列执行，旧库那时还没有 `category` 列，索引语句会直接报错。

`idx_analyzed_recency` 是**表达式索引**（SQLite ≥ 3.9，容器 python:3.12-slim 自带 ≥ 3.40），专门支撑 `search_candidates` 与回填候选集的 `ORDER BY COALESCE(published_at, analyzed_at) DESC, article_id DESC`。它的收益远大于「省掉一次排序」：没有索引时 SQLite 必须先算完全部命中行才能排序，于是每行的 `py_norm`（6000 字正文的归一）都会跑一遍，哪怕最后只留 `limit` 行；有索引后按索引顺序取满 `limit` 即停。实测 2 万行全命中、正文 6000 字、`limit=2000`（本机 min-of-2）：**标题命中 3.9s → 0.07s，仅正文命中 11.9s → 0.82s**。两者差一档，因为预筛的 `OR` 在标题命中后短路、根本不归一 6000 字正文——**用户查的行业词/细分词大多只在正文出现，别把 0.07s 当成常态成本**（与 §5 性能表同一条口径）。查询计划从 `SCAN a + TEMP B-TREE FOR ORDER BY` 变成 `SCAN a USING INDEX idx_analyzed_recency`。只测 `ORDER BY` 子句（不带 `py_norm`）会得到 0.01s → 0.002s，严重低估收益——那个计划本来就不用归一被丢弃的行。另注意用「命中数 ≤ limit」的数据测不出差异（那时两种写法都要全扫、都不截断）。

`idx_analyzed_asc_undated_last` 专门支撑 `published_asc` 的截断键（`(_TIME_EXPR IS NULL), _TIME_EXPR ASC, article_id ASC`）：升序时 SQLite 默认把 NULL 排最前，无日期行会占掉「最早 2000 行」的名额，所以查询必须加 `IS NULL` 前缀；前缀让排序表达式与 `idx_analyzed_recency_utc` 不同，没有配套索引时 2 万行全命中会退化为 `SCAN a + TEMP B-TREE FOR ORDER BY`（实测 2.0s → 0.05s，索引扫描可提前 `LIMIT`）。

### 7.2 注意事项

- **幂等替换**：`save_analysis()` 先 UPSERT 主行，再删旧 `article_tags`/`extractions` 并重插；同一 `article_id` 重跑不会产生重复行；
- **分类是「保留式」UPSERT**：`category` 传空串（= 本次没做分类）时由 `COALESCE` 保留库里已有的分类，不会被清成 NULL；明确判成 `other` 时写的是 `'other'`（非空值），因此能正常覆盖。代价是**没有「显式清空分类」的代码路径**——清空目前只能手写 SQL 或删行重分析，需要时应提供一次性脚本而不是复用空串；
- **三字段要同传**：`category`/`category_confidence`/`category_engine` 各自独立 `COALESCE`，只传其中一部分会得到「新类 + 旧置信度」这种拼接结果。唯一生产调用方 `_persist_result` 总是三字段一起传，新增调用方必须照此约定；
- **批量提交**：`save_analysis(commit=False)`/`save_checklist(commit=False)` 由调用方统一 commit；没有外层事务时会先 `BEGIN`（否则 SAVEPOINT 立即提交）；
- **`verified` 语义**：只表示「值能在原文定位」，不表示语义正确。LLM 抽取值经 `_locate()` 校验才可能 `verified=true`；规则引擎产出的一切恒为 `false`；
- **连接**：`connect()` 先 `PRAGMA busy_timeout=30000`，只在当前不是 WAL 时才切 `journal_mode=WAL`（每次连接都执行 WAL 会多拿一次写锁）；
- **迁移**：没有版本号，靠 `_migrate_columns()` 幂等补列——`PRAGMA table_info` 查已有列，缺则 `ALTER TABLE ADD COLUMN`（`_ADDED_COLUMNS` 声明期望的列与类型）。执行顺序固定为 `executescript(SCHEMA_SQL)` → `_migrate_columns()` → `executescript(_POST_MIGRATION_SQL)` → commit：依赖新列的索引必须放 `_POST_MIGRATION_SQL`，放 `SCHEMA_SQL` 会在老库上因列不存在而失败。**新增列要同时改 `SCHEMA_SQL`（新库直接建全）和 `_ADDED_COLUMNS`（老库补列）**，只改一处会导致新老库结构不一致；改字段类型或加约束仍必须写一次性迁移；
- **无外键**：删除顺序由代码保证（子表 → `analyzed_articles` → `runs` → 已完成 `analysis_jobs`），`prune_old_data()` 在一个事务内执行，失败整体回滚；
- **保留期**：默认 `RETENTION_DAYS=90`，启动时、每次成功分析后、每次 job 成功后自动硬删除；`0` 表示永久保留；`VACUUM` 单独执行（不能在事务里）；
- **正文列的覆盖语义**：`body` 走 `COALESCE(NULLIF(excluded.body, ''), body)`，与 `category` 同属「本次没拿到就保留旧值」；区别是 `body` 没有显式清空路径（正文是上游副本，清空没有业务意义）。回填走 `set_body()` 只 UPDATE 这一列，不碰 `analyzed_at` 与标签；
- **迁移只覆盖含 `content_length` 的库**：`_ADDED_COLUMNS` 是穷举式的显式清单，现声明 `body` 与分类三列共四列。更老的、连 `content_length` 都没有的库不在支持范围内（`SCHEMA_SQL` 对已存在的表不会补列）；
- **降级模式下分类与标签可能不同源**：`categories.json` 缺失时 pipeline 退回单级打标（全词表），此时 `category` 为空 → 保留旧分类，但 `article_tags` 会被整体替换成全词表结果。于是可能出现 `category='project_application'` 却带 `ipo`/`tender` 之类类外标签的行。这是「移走词表文件后 force 重跑」这一罕见运维动作的已知取舍，恢复词表后再跑一次即回到类内一致状态；`tests/test_categories.py::test_single_stage_rerun_keeps_category_but_widens_tags` 固定了该行为。

---

## 8. CLI 命令速查

所有命令都通过 Docker 容器执行（在宿主机项目根目录运行，无需本地安装 Python）：

```bash
docker compose run --rm insight python -m app <子命令> [args]
```

| 子命令 | 说明 |
| --- | --- |
| `analyze --limit 10 [--force] [--concurrency 100]` | 跑一批分析；结束后自动执行一次 prune（失败只记日志） |
| `prune [--days 90] [--dry-run] [--vacuum]` | 删除超过保留期的分析结果；先 dry-run 看数量 |
| `init` | 只建库建表 |
| `backfill-bodies [--limit 500] [--batch 50]` | 给历史行补 `analyzed_articles.body` 与 `body_html`：逐篇调上游 detail 取 `raw_content or content` 与 `body_html`，只 UPDATE 两列且不覆盖已有值，不重跑模型、不改 `analyzed_at`；可重复执行（只挑任一侧为空的行），单篇失败不中断整批，打印 `{"filled","html_filled","failed","skipped","remaining"}`；`demo` 模式直接拒绝（不许碰网络）；候选与分批都按「最新优先 + `article_id` 兜底」取，因此死链不会永久占住小 `--limit` 窗口；每批先取回全部正文再开写事务，网络请求期间不持有未提交事务 |
| `backfill-industries [--limit 500] [--workers N]` | 给历史行补受控行业：只写 `industry`/`industry_confidence`/`industry_engine`，不重跑标签/抽取/清单；可重复执行（只挑 `industry` 为空的行），打印 `{"scanned","analyzed","skipped","errors","remaining","engines"}` |
| `health` | 打印当前配置（llm_enabled / 数据源模式 / news_base_url / db 路径 / 保留天数） |

示例：

```bash
# 小批量试跑
docker compose run --rm insight python -m app analyze --limit 5
# 定时批处理（目标 1 万篇/天以内，配合 ANALYZE_CONCURRENCY 调优）
docker compose run --rm insight python -m app analyze --limit 10000 --concurrency 100
# 清理（先看再删）
docker compose run --rm insight python -m app prune --dry-run
docker compose run --rm insight python -m app prune --vacuum
```

---

## 9. 部署机制

### 9.1 Docker（唯一部署方式）

- 文件：`Dockerfile`、`.dockerignore`、`docker-compose.yml`；
- 基础镜像 `python:3.12-slim`，`pip install .`，复制 `app/` 与 `fixtures/`；
- 容器以 **uid 10001 的 `insight` 用户**运行，`/app/data` 已 chown；
- `EXPOSE 8020`；`HEALTHCHECK` 用 `urllib` 打 `http://127.0.0.1:8020/health`；
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020"]`。

`docker-compose.yml` 关键点：

- 端口：`${INSIGHT_BIND:-127.0.0.1}:${INSIGHT_PORT:-8020}:8020` —— **默认只绑回环**，因为 `POST /v1/analyze` 会消耗 LLM 额度；
- 数据卷：`${INSIGHT_DATA_DIR:-insight_data}:/app/data` —— 默认命名卷。bind mount 的 `./data` 由 docker daemon 以 `root:root` 创建，而容器以 uid 10001 运行，Linux 上首次启动会因 SQLite 无法读写而崩溃循环；想直接看库文件须先 `mkdir -p data && sudo chown 10001 data`；
- `extra_hosts: host.docker.internal:host-gateway` —— Linux 下容器访问宿主机爬虫 API 需要；
- `environment` 是**逐条显式列出**（当前 22 个，见 `docker-compose.yml:11-42`）：compose 不支持 `NEWS_*` 这类前缀通配，`app/settings.py` 新增字段必须手工追加一行，否则运行期静默走默认值；`.env` 不进镜像也不进 Git；
- `restart: unless-stopped`。

### 9.2 CI 与仓库规则集

`.github/workflows/ci.yml` 在 PR 与 master 推送时运行三个 job（均为合并前必须通过）：

| job | 内容 |
| --- | --- |
| `python 3.10`–`3.14` | `compileall` + `ruff check .` + `mypy`（仅 3.12）+ `pytest`，五个版本矩阵 |
| `yaml lint` | `python tools/yaml_lint.py`：拒绝重复映射键、支持多文档、不误杀 `<<` 合并键 |
| `docker build + container smoke` | `docker compose config` + `docker build` + 容器内跑 pytest + 启动容器请求 `/health` |

仓库规则集 `pr-protection`（master）：禁止删除分支与强推；要求 `CodeRabbit`、`python 3.10`–`3.14`、`yaml lint`、`docker build + container smoke` 共 8 个检查全绿；要求所有 review thread 已解决；`required_approving_review_count=0`（GitHub 不允许仓库所有者批准自己的 PR，设 1 会永久卡死单人维护的 PR）；保留 `bypass_actors`（RepositoryRole / always）。

**bypass 仅允许一种情形**：CodeRabbit 侧故障或配额耗尽导致 required check 永久 pending（它返回 `skipped` / `Review paused`，GitHub 视为通过，但必须人工确认本地 `ruff check .`、`mypy`、`pytest -q`、`docker build` 全部通过后再合入）。GitHub Actions 的检查不允许 bypass —— 它们本地可复现。

---

## 10. 测试策略

### 10.1 现有测试资产（`tests/`，17 个文件）

| 文件 | 覆盖 |
| --- | --- |
| `test_api.py` | 鉴权（open/401/两种头）、checklist 端点、`/v1/analyze` limit 上限、异步 job 生命周期、`/v1/categories` 词表与按类过滤、`/v1/tags?category=` |
| `test_notifications.py` | 项目申报公告生成通知、未读/单条已读/全部已读、`--force` 重分析不重复通知、404 |
| `test_categories.py` | 分类词表自洽（末位 other、other.tags 为空、引用标签都存在、新增业务标签必须被某类引用）、词表外大类回落兜底且 conf=0、非 dict payload 回落、置信度夹取、并列按声明顺序、缺 `categories.json` 退回单级、类内打标白名单交集、兜底类只留 `domain` 组、分类 UPSERT 保留式写入、降级重跑保留分类但标签退回全词表、老库迁移补列与索引幂等、规则引擎用原始分比较（高分先命中不被低分翻盘）、词表缺兜底类时告警 |
| `test_tagging.py` | 词表外标签丢弃、去重、max_tags 截断、规则引擎多标签 |
| `test_extraction_rules.py` | 日期区间取右端、裸开始日期不当截止、中文两位数、闰日、规则引擎不标 verified |
| `test_extraction_llm_range.py` | 13 个区间用例：右端仅「日」、跨年、多区间取最后、截止前缀优先、电话/时钟不误判、跨逗号不误取评审区间 |
| `test_deadline.py` | 绝对日期、相对发布日期、无基准不猜测、月日跨年、月底、未知文本 |
| `test_checklist.py` | 规则顺序、缺字段跳过、verified 传递、列表扁平化、上限保留截止项、LLM 丢弃不可定位/编造事实/过短依据、数字子序列误判、引擎降级、缺清单补生成、JSON 损坏 |
| `test_pipeline.py` | demo 端到端、重跑跳过、`field` 与 `due_before` 正交、空正文不入库、单篇失败不拖垮、失败提交只记已提交数、auto 回退原因写入 runs |
| `test_concurrency.py` | 并发批次全部分析、小批量仍全部提交、`commit=False` 真正延迟提交 |
| `test_jobs_prune.py` | job 执行与结果、队列上限 429、锁占用快速失败、stale job 标记与复用、prune 各表 dry-run/真删、保留 0 天 |
| `test_llm.py` | 429 重试成功、非对象 JSON 重试、401 不重试 |
| `test_news_client.py` | `published_desc`、page_size 不超过上游 100、live 报错、auto 回退记录原因、分页、坏 `total_pages`、坏 `content_length`、详情异常不崩、4xx 不回退、去重、详情覆盖顺序 |
| `test_search.py` | 检索层纯逻辑（`:memory:` 直插数据）：候选截断前按最新定序且保留最新 N 行、`published_at` 为空时按 `analyzed_at` 参与定序、短语映射闭集标签、子片段权重低于整词、空查询、未知词回落字面检索、标签命中压过纯正文命中、分数有界 `[0,1]`、无信号被过滤、按类分组且类内降序、未知词空 groups、正文命中不跨类复制、`per_category` 截断但 `count` 报全量、`categories` 过滤、默认不返回 body 且按需返回、`matched_tags`/`terms` 可排障、非 ASCII 大小写（Ärger/ÖBB/MÜLLER）不敏感、`\u2003`/`\u2009`/`\u2028` 等窄空格被折掉、同一处文本只计一次（含重叠出现）、`matched_terms` 不含被长形态吃掉的形态、高频短词记账不退化成 O(n²)、候选集截断只在真丢行时告警 |
| `test_search_api.py` | `GET /v1/search` 的 HTTP 契约：未带 key 401、返回 groups、类顺序符合 `categories.json`、类内降序、未知词空 groups、`q` 为空 422、默认无 `body` 字段、`include_body=true` 时有 `body`、`category` 过滤生效 |
| `test_yaml_lint.py` | 合法/重复键/非法 YAML、SKIP 集合与顶层目录语义、工作目录无关、非 UTF-8、仓库全部 YAML 通过 |
| `test_backfill.py` | 回填正文与正文 HTML：demo 模式拒绝访问网络、网络请求期间不持有未提交事务（读**主连接**的 `in_transaction`，不是新连接；必须 `batch>1` **且行数 > batch**，否则只覆盖到「单 chunk 边取边写」而漏掉「事务跨过下一批请求」）、`--limit 1` 下死链不钉死窗口（扫描预算与写入上限解耦）、`limit<0`/`batch<1` 在发请求前被拒绝（SQLite 把 `LIMIT -1` 当成不限）、候选截断前排序确定、已有正文的行不被覆盖、已有正文但缺 HTML 时只补 HTML |
| `test_industries.py` | 受控行业：词表 10 项与缺失文件、规则引擎标题优先与 other 兜底、LLM 表外 id 丢弃并夹紧置信度、LLMError 降级规则、save_analysis 行业落库、set_industry 不覆盖已有值、backfill-industries 只补空行且负数 limit 拒绝、检索按行业过滤、/v1/industries 与 /v1/articles?industry= |

基线：宿主机全量 `pytest` **198 passed, 1 warning**（StarletteDeprecationWarning）；只挂 `tests/`（不挂 `tools/`）时相应减少。

**宿主机跑 pytest 前必须清代理变量**：本机 `NO_PROXY` 形如 `localhost,...,[::1]` 会被 httpx 当成 mounts 模式解析，在 `httpx.Client` 构造处抛 `InvalidURL: invalid literal for int() with base 10: ':1]'`，表现为 19 个与代码无关的失败。`$env:X=''` 清不掉（PowerShell 空串赋值等于删除，子进程仍继承），要显式删除：

```powershell
Remove-Item Env:NO_PROXY,Env:no_proxy,Env:HTTP_PROXY,Env:HTTPS_PROXY,Env:http_proxy,Env:https_proxy -ErrorAction SilentlyContinue
```

### 10.2 运行方式

```bash
# 宿主机（推荐；dev 依赖已固定版本，与 CI 一致）
pip install -e ".[dev]"
ruff check .
mypy
python tools/yaml_lint.py
pytest -q

# 或在镜像里跑（tests/ 与 tools/ 不进镜像，必须挂载；镜像不带 pytest）
docker compose run --rm -v "$(pwd)/tests:/app/tests" -v "$(pwd)/tools:/app/tools" \
  --entrypoint sh insight -c "pip install --user -q pytest pyyaml && python -m pytest tests -q"
```

### 10.3 提交前必须执行

```bash
docker compose build
docker compose run --rm -v "$(pwd)/tests:/app/tests" -v "$(pwd)/tools:/app/tools" \
  --entrypoint sh insight -c "pip install --user -q pytest pyyaml && python -m pytest tests -q"
docker compose up -d --force-recreate && curl -f http://127.0.0.1:8020/health
```

有本地 Python 环境时再补 `ruff check .`、`mypy`、`python tools/yaml_lint.py` —— CI 用同样的命令卡住不通过的 PR。

### 10.4 测试缺口

- 没有真实 LLM 的契约测试：LLM 客户端用 mock，模型输出漂移无法在 CI 捕获；
- 真实上游联调依赖 `news-crawler` 与真实站点，CI 只跑 demo 模式；
- 并发压力未纳入 CI（设计目标 1 万篇/2 小时，但为避免消耗额度不做常驻压测）；
- `prune` 只验证计数与表清空，未验证大库 VACUUM 的耗时与磁盘回收效果。

---

## 11. 关键设计决策与已知问题

### 11.1 设计决策（改动前先理解）

1. **先分大类，再在类内闭集打标。** 一级分类把标签空间切成互不重叠的几块：模型先选唯一一类，第二级只能看到该类允许的标签（`categories.json` 的 `tags` 白名单）。好处是跨类污染从提示词层面就消失了（人才通知看不到资金补贴标签），代价是类别边界要人工维护；归到兜底类 `other` 的文章跳过第二次模型调用，只用规则引擎保住 `group=domain` 的行业标签，避免「不属于 5 大类」等于「一无所知」。
2. **闭集打标，标签外一律丢弃。** 放任模型自造标签，会把 `Artificial Intelligence` 和 `AI` 变成两个标签——这是 [karakeep](https://github.com/karakeep-app/karakeep) issue #111 记录的原始问题，#529 的解法是「只允许使用用户已建的标签」。本项目在 `tagging.py` 做了同样的守卫。标签集合与「标签→schema 路由」借鉴 [autolabel](https://github.com/refuel-ai/autolabel) 的闭集思路。
3. **打标与抽取分离。** 打标输出空间闭合（必须 `in` 词表），抽取输出空间固定（必须 schema 内字段）；打标漏标只是少一条线索，抽取编造会污染下游。因此两者不同 prompt、不同校验。
4. **相对时间以发布时间为基准。** 「自发布之日起30日内」「本月底」若用抓取时间换算会随重跑漂移；抽取提示词显式传入 `published_at_utc`（缺失回退 `published_at`），`normalize_deadline()` 按此推算。
5. **缺失字段返回 null，值要能在原文定位。** `_locate()` 做空白无关子串校验；定位不到保留但标 `verified=false`，供人工复核。模型给出的日期用原文重算，对不上就降级。规则引擎产出恒为 `verified=false` —— 正则只证明文本出现过，不证明语义正确（例如「报名时间3月1日至3月20日，评审时间4月1日至4月10日」里哪个才是截止，只有语义能判断）。
6. **结果独立成库。** 见 §1 对照表：SQLite 单写 + 许可证隔离 + 失败隔离。
7. **清单是派生视图，不是新事实。** 清单项要么引用已抽取事实（`basis="excerpt"`：原文片段必须能在正文定位，且 `text` 里的日期/金额/电话等事实 token 必须被该片段支持），要么显式标 `suggested`（盖章、留存回执这类流程建议，禁止出现任何数字）。规则生成的项沿用抽取行的 `verified`，不冒充已核验事实。清单单独存 `article_checklists`，重新生成不重写标签/抽取表。
8. **单篇失败不拖垮批次，但「没拿到正文」不算成功。** 见 §4 并发模型的第 4、5 条。
9. **检索排序由后端定，且是可解释的人工权重。** 搜索界面由前端实现，但前端拿不到标签置信度、默认也拿不到正文，没有重排的依据，所以 `groups[].items[]` 的顺序就是最终顺序。权重 0.70/0.20/0.10 和为 1，总分天然落在 `[0,1]`，可当前端的百分比直接用。刻意不用学习排序：一是没有标注数据，二是排序规则必须能被运维读懂并原地调整。覆盖度是**长度惩罚**而非语义消歧——「人才申报」里两类标签各覆盖一半、权重相同，区分它们的是标签置信度与标题命中。

### 11.2 已知问题与技术债

当前已知问题与技术债：

1. **抽取质量取决于模型与 prompt。** 本项目只保证「不越界」（不造标签、不造字段、值可定位），不保证召回；prompt 与模型变更必须用真实样本回归。
2. **相对截止时间覆盖有限。** 只覆盖 `N日内`/`N个工作日`/`本月底` 等常见表述，「公示结束后5个工作日」这类链式表述仍会漏。
3. **分析作业进程内全局串行。** 同步接口与异步 job 共用 `ANALYZE_LOCK`，单进程单 SQLite，不适合多副本水平扩展；要横向扩展需外部队列（Redis/Celery 等）并改存储层。
4. **每篇最多 4 次 LLM 调用（当前单 schema 下）。** 一级分类 1 次 + 类内打标 1 次 + 每个命中 schema 各 1 次抽取 +（命中申报时）清单 1 次；新增 schema 后按命中数线性增加，估算额度与并发时不能仍按 4 次算。归到兜底类 `other` 的文章只有 1 次（分类），不打二级标签；`categories.json` 缺失时退回单级打标，仍是原来的 3 次。吞吐取决于供应商并发与限流。`ANALYZE_CONCURRENCY` 只是本地上限，`LLM_MAX_CONCURRENCY` 另外限制真正同时在途的供应商请求；429/5xx 优先按 `Retry-After`、否则指数退避加抖动重试。
5. **保留期是硬删除。** `RETENTION_DAYS` 默认 90，不保留归档；`runs` 与已完成的 `analysis_jobs` 也一起清。
6. **数据库没有版本号，只有幂等补列。** `_migrate_columns()` 靠 `PRAGMA table_info` 补缺失列（见 §7.2），加列是自动的；改类型、加约束、改索引语义仍需手写一次性迁移。升级后历史行 `category` 为 `NULL`，要拿分类得 `analyze --force` 重跑。
7. **演示数据与规则引擎只用于跑通链路。** 不作为效果基线；生产必须配置 `LLM_API_KEY`。
8. **检索是字面 + 闭集标签的混合，没有分词与同义词扩展。** 同义关系靠 `tags.json` 的 `keywords` 打通（「补贴/资助」同属一个标签），拼音、错别字、跨词序都不容忍；候选集上限 2000 行，命中面极广的泛词会被截断。要更强的相关性得上分词或向量检索，那会引入新依赖与新的排序口径，需要单独决策。
9. **大类边界靠人工维护，双命中会丢另一类的二级标签。** 规则引擎按关键词计分单选，遇到「专项资金申报通知」这类同时像资金补贴与项目申报的文章会归到分数更高的一类，另一类白名单内的标签随之拿不到（`funding_subsidy` 的白名单不含 `project_application`，这类文章不会触发申报抽取）。需要时给该类补标签，或改用 LLM 分类（已在有 key 时生效，规则仅在降级时使用）。

---

## 12. 多 Agent 协作规范

### 12.1 分支与提交

- 每个 Agent 独立分支：`feat/xxx`、`fix/xxx`、`docs/xxx`、`ci/xxx`；
- 不直接改 `master`，通过 PR 合并；
- 提交信息使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`test:`、`refactor:`、`chore:`；
- 合并前必须通过 CI 与 `docker compose build` + 容器冒烟。

### 12.2 建议的模块分工

| 工作流 | 负责范围 | 主要文件 |
| --- | --- | --- |
| A. 分类与打标 | 大类边界、标签语义、规则引擎、prompt | `app/tagging.py`、`app/vocab/categories.json`、`app/vocab/tags.json` |
| B. 抽取与 schema | 字段定义、日期归一、定位校验 | `app/extraction.py`、`app/vocab/schemas/` |
| C. 清单 | 清单生成、依据校验 | `app/checklist.py` |
| D. 管道与并发 | 批次编排、线程池、批量提交、作业 | `app/pipeline.py`、`app/jobs.py` |
| E. API 与配置 | 路由、鉴权、错误体、`Settings` | `app/main.py`、`app/settings.py` |
| F. 存储与保留 | schema、读写函数、prune | `app/db.py`、`app/prune.py` |
| G. 工程化 | Docker、CI、文档、发布 | `Dockerfile`、`docker-compose.yml`、`.github/`、`README.md`、`AGENTS.md` |

### 12.3 文件冲突风险

- `app/pipeline.py` 是最核心、最易冲突的文件，修改前先同步最新 `master`；
- `app/settings.py` 新增字段时，必须同步 `docker-compose.yml` 的 environment、`.env.example` 与 README 的吞吐/配置表；
- `app/vocab/tags.json` 的 `id` 是持久化契约，**改名不改 id**；新增标签不要复用已删除的 id；
- `app/vocab/categories.json` 的 `id` 同样是持久化契约（落 `analyzed_articles.category`），**改名不改 id**；调整 `categories` 的**声明顺序**会改变规则引擎的并列归属，属于行为变更；
- `app/db.py` 的 `SCHEMA_SQL` 与迁移顺序耦合，改表结构要同时考虑老库升级；
- `tests/` 与 `tools/` 不进镜像，CI 用挂载方式跑；改动测试时要确认容器内也能跑通。

### 12.4 共享接口约定

- `Settings`（frozen dataclass）：字段只从环境变量读取；`db_path=data_dir/insight.db`、`llm_enabled=bool(llm_api_key)`；
- `NewsClient.list_articles(limit: int, offset: int = 0) -> tuple[list[NewsArticle], str]`：返回实际使用的模式（`live`/`demo`）；`page_size` 不得超过上游上限 100；4xx 不得回退演示数据；
- `NewsClient.hydrate_bodies(articles: Iterable[NewsArticle], mode: str) -> list[NewsArticle]`：live 模式补正文；详情失败标 `body_ok=False`，不抛异常；
- `UpstreamError(message, *, status_code: int | None = None, fallbackable: bool = True)`：只有网络/5xx 允许 auto 回退；
- `LLMClient.chat_json(system: str, user: str, *, retries: int = 2) -> dict[str, Any]`：无 key 时 `available=False` 且抛 `LLMError`；调用方自行降级；
- `load_categories(vocab_dir: Path) -> tuple[dict[str, Any], list[CategoryDef]]`：读 `categories.json`，`lru_cache(maxsize=4)`；文件缺失返回 `({}, [])`，pipeline 据此退回单级打标；
- `classify_article(settings, llm, categories, title, body) -> CategoryHit`：单选；`categories` 为空时返回 `engine="none"`；LLM 不可用/失败降级规则引擎；`CategoryHit.confidence` 恒在 `[0, 1]`；
- `tags_for_category(categories, tags, category_id) -> list[TagDef]`：该类白名单与全量词表的交集，空列表表示该类不打二级标签；
- `tags_for_unclassified(tags) -> list[TagDef]`：兜底类专用，只保留 `OTHER_RULE_GROUPS = {"domain"}` 的行业标签；
- `tag_article(settings, llm, tags, title, body) -> tuple[list[TagHit], str]`：engine 为 `llm` 或 `rules`；**传入的 `tags` 必须是已按大类收窄的范围**，不要把全量词表直接传进来；
- `extract_article(settings, llm, schemas, hits, title, body, *, published_at_utc: str | None, published_at: str | None) -> list[ExtractionResult]`：未命中 `trigger_tags` 返回空列表；
- `build_checklist(llm, title, body, fields, *, raw_texts=None, verified=None, body_chars=6000) -> ChecklistResult`；
- `analyze_batch(settings, *, limit: int, force: bool = False, concurrency: int | None = None) -> dict`：返回 `{run_id, mode, scanned, analyzed, skipped, errors, tag_engines, llm_enabled}`；批次开始时读一次 `load_categories()`，整个批次共用同一份分类词表；
- `db.save_analysis(..., commit: bool = True)` / `db.save_checklist(..., commit: bool = True)`：`commit=False` 由调用方统一提交，且必须处于同一事务；
- `create_app(settings: Settings | None = None) -> FastAPI`：测试可注入独立设置；
- `POST /v1/jobs` 的 body schema（`type`/`limit`/`force`/`concurrency`）属于对外契约，新增字段必须保持向后兼容；
- 数据库字段与 `runs`/`analysis_jobs` 状态语义保持向后兼容，新增字段需要迁移策略；`analyzed_articles.category` 为 `NULL` 表示「分类上线前分析的历史行」，不得用 `other` 代替——两者在统计与过滤上语义不同。
- `db.search_grouped(conn, *, raw_query, tags, category_order=(), categories=None, sources=None, published_from=None, published_to=None, per_category=20, include_body=False, sort="relevance") -> dict`：检索入口。返回 `{"query", "matched_tags", "terms", "groups": [{"category", "count", "items"}], "total", "returned", "truncated", "undated_excluded", "sort"}`。四个统计键的语义：`total` = 候选集（上限 `_CANDIDATE_LIMIT`=2000 行）内的命中数，`truncated=true` 时它只是下界；`returned` = 实际渲染条数（每类被 `per_category` 截断后），与 `total` 的差额即"还有更多没显示"；`undated_excluded` = 带日期过滤时"命中关键词与分类/来源条件、但两个时间列都判不出日期"而被排除的篇数（不带日期过滤时恒为 0，是 `COUNT(*)` 因而不受候选集截断影响，统计条件是 SQL 预筛命中，未过 `search.has_signal`）；**空查询（`q` 归一后为空）走早退分支，返回同样的键集合**，前端不必为它单独兜默认值。`count` 是漏斗后的全量命中数，`items` 被 `per_category` 截断。**返回顺序即最终展示顺序**（类按 `category_order`；类内顺序由 `sort` 决定：`relevance` 按 `score` 降序、`published_desc`/`published_asc` 按真实时刻 `_TIME_EXPR` 升/降序，两种模式都以相关度 `score` 为第一同分键、`article_id` 为最终兜底），改动排序等于改动前端可见行为；
- `search.resolve_query(raw: str, tags: list[TagDef]) -> Query` / `search.score_article(query, *, title, body, tag_confidences) -> Relevance` / `search._masked_hits(text, terms) -> tuple[int, float, list[str]]`：纯函数，无 I/O，可直接单测。纯函数，行为细节（标记数组记账、重叠只计一次、`+1` 步进的用意）见 §5 排序契约中的命中计数条目，此处只固定契约：按 `terms` 顺序（`resolve_query` 已按权重降序输出）逐个计数，同一处文本只计一次；第三个返回值是实际贡献命中的形态，供 `matched_terms` 使用（被长形态吃掉的短形态不列）；第三个返回值是实际贡献命中的形态，供 `matched_terms` 使用（被长形态吃掉的短形态不列）；
- `db._ensure_norm_func(conn)`：把 `search._norm` 注册为 SQL 函数 `py_norm`，预筛与打分共用同一份归一实现。新增涉及 `instr` 预筛的查询时必须先调用它，否则报 `no such function: py_norm`。权重常量在 `app/search.py` 顶部，改动须保持 `W_TAG + W_TITLE + W_BODY == 1.0`（否则 `score` 不再落在 `[0,1]`）并补用例；
- `db.set_body(conn, *, article_id, body, commit=True)`：只 UPDATE `body` 一列，不碰 `analyzed_at` 与标签，供回填使用；正文写入走 `COALESCE(NULLIF(excluded.body, ''), body)`，本次没拿到就保留旧值；
- `backfill_bodies(settings, *, limit=500, batch=50) -> dict`：给 `body IS NULL OR body = ''` 的历史行补正文，只搬文本不重跑模型；可重复执行，单篇失败不中断整批，返回 `{"filled", "failed", "skipped", "remaining"}`。`limit` 是**写入上限**，扫描预算为 `limit * _SCAN_FACTOR`（`_SCAN_FACTOR = 5`），否则不可回填的行（死链、上游无正文）会永久占满小 `--limit` 的窗口。这是**有界**的权衡：连续 `5×limit` 篇不可回填时仍会退回每轮重试同一批（实测 `--limit 1` 下 4 篇连续死链可达更早行、5 篇不可达），此时应调大 `--limit` 或先处理死链；彻底解决需要跨进程游标/失败清单，本命令刻意不做（避免库外隐式状态）。`remaining` 含这类行，不一定归零，**且 `remaining` 稳定不能单独作为「回填完成」的依据**：它稳定既可能是「只剩不可回填的行」，也可能是「被扫描预算卡住、每轮重试同一批、后面还有可回填的文章」。判据必须看「本轮是否扫描覆盖了全部剩余候选」，不能只看 `filled` 是否为 0：日志「已回填 X/Y（候选 Z）」的 `Z = 5×limit` 是本轮实际扫过的候选数，**`Z < remaining` 时 `filled=0` 什么都证明不了**（这一轮压根没扫完），`Z ≥ remaining` 且 `filled=0` 只代表**扫描覆盖完成**，不等于回填完成——还要看 `failed`：`failed > 0` 是上游临时故障（超时/5xx/连接失败），稍后重跑；`failed == 0` 才是上游明确无正文（`skipped`），此时可停。`skipped` 涵盖「请求成功但正文为空」与「返回 404/410」（`_GONE_STATUS_CODES`，死链重跑也不会有），`failed` 是超时/5xx/连接失败这类临时故障。反例（实测）：250 篇死链 + 1 篇可回填且可回填者在最旧处时，`--limit 1`（Z=5）与 `--limit 50`（Z=250）都是 `filled=0 / remaining=251`，调大 `--limit` 后 `filled` 没有变大，但回填并未完成——预算恰好被死链吃满。对比：5 篇死链 + 1 篇可回填时 `--limit 1` 得 `filled=0/remaining=6`、`--limit 50` 得 `filled=1/remaining=5`（此时 Z 够了）。`demo` 模式直接抛 `RuntimeError`（`NewsClient.get_article` 只有 live 路径，会违反 settings 的「demo 不碰网络」承诺）。参数非法抛 `BackfillUsageError`（`ValueError` 子类，CLI 只捕它 + `RuntimeError` 并返回 2；不捕裸 `ValueError`，否则内部意外 bug 会被静默成退出码 2）：`limit < 0`（SQLite 把 `LIMIT -1` 当不限，会回填整库）、`batch < 1`（`range(0, n, 0)` 中途抛错），都在 `mkdir`/`init_db` 与任何网络请求之前拒绝；`limit=0` 合法，等于空跑。

### 12.5 隐私与安全红线

- 不提交任何 `.env`、LLM key、`INSIGHT_API_KEY`、`NEWS_API_KEY`、真实抓取数据；
- 不在日志中打印完整请求头、Authorization 或 prompt 全文（`llm.py` 只截取响应体前 300 字符用于排障）；
- `/health` 等未鉴权端点不得回显内网地址；
- 演示数据（`fixtures/`）与测试里的 key 必须是假值；
- 生产部署默认只绑 `127.0.0.1`；对外暴露前必须设置 `INSIGHT_API_KEY`。

---

## 13. 发布流程

1. 确认 `master` 上所有改动已通过 CI（三个 job 全绿）；
2. 同步更新版本号（三处必须一致）：`pyproject.toml` 的 `version`、`app/__init__.py` 的 `__version__`、`docker-compose.yml:5` 的 `image: news-insight:x.y.z`；同时更新 `AGENTS.md` §1/§2 中的版本引用；
3. 提交：`release: 升级版本至 x.y.z`；
4. 推送 `master`，等待 `ci` 工作流成功；
5. 打标签并推送：

   ```bash
   git tag -a vX.Y.Z -m "news-insight vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. 创建 GitHub Release，写清新增功能、修复内容与验证情况。

---

## 14. 快速上手命令（Agent 版）

```bash
# 1. 同步代码
git pull origin master

# 2. 准备环境（.env 不进库）
cp .env.example .env   # 填 LLM_API_KEY；不填也能跑（规则引擎降级）

# 3. 构建并启动
docker compose up -d --build
curl -f http://127.0.0.1:8020/health

# 4. 测试（tests/ 与 tools/ 需挂载，镜像不带 pytest）
docker compose run --rm -v "$(pwd)/tests:/app/tests" -v "$(pwd)/tools:/app/tools" \
  --entrypoint sh insight -c "pip install --user -q pytest pyyaml && python -m pytest tests -q"

# 5. 小批量分析
curl -s -X POST "http://127.0.0.1:8020/v1/analyze?limit=5" -H "X-API-Key: $INSIGHT_API_KEY"

# 6. 长批次异步作业
curl -s -X POST "http://127.0.0.1:8020/v1/jobs" \
  -H "X-API-Key: $INSIGHT_API_KEY" -H "Content-Type: application/json" \
  -d '{"limit":1000,"concurrency":100}'

# 7. 查结果
curl -s "http://127.0.0.1:8020/v1/extractions?field=deadline" -H "X-API-Key: $INSIGHT_API_KEY"
```
