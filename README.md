# news-insight

把 [news-crawler](../news) 抓到的新闻，变成**结构化、可查询的情报**：

1. **打标签** —— 只能从你自己写好的标签词表里选（闭集多标签），模型不许自己造标签；
2. **抽关键线索** —— 命中 `project_application` 这类标签后，进入 schema 驱动的事实抽取，例如项目申报的申报主体、材料清单、**截止时间**、资助额度、申报渠道；
3. **落库 + 出接口** —— 结果存独立 SQLite，通过 HTTP API 查询，供后续业务系统消费。

它不抓网页、不解析正文、不碰爬虫的数据库。与 news-crawler 之间只有一条 HTTP 契约。

```
news-crawler (GPL-3.0)                    news-insight (MIT)
┌──────────────────────┐   GET /v1/articles   ┌────────────────────────────┐
│ GNE 抽取 + SQLite     │ ───────────────────► │ tag（闭集多标签）            │
│ FastAPI /v1          │   GET /v1/articles/{id}│   └─► 标签命中 schema？     │
│ /app/data/news.db    │                      │        └─► extract（schema） │
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
curl -X POST "http://localhost:8020/v1/analyze?limit=10"
curl "http://localhost:8020/v1/extractions?schema=project_application&due_before=2026-06-30"
```

不启动爬虫也能验证：`NEWS_SOURCE_MODE=demo` 时使用 `fixtures/articles.json` 的内置新闻（含两篇申报公告、一篇降准、一篇融资）。

### 三种数据源模式

| 模式 | 行为 |
| --- | --- |
| `auto`（默认） | 先请求爬虫 `/v1`；连不上则回退内置演示数据，并在 `/v1/runs` 里记录实际模式 |
| `live` | 只走真实上游，失败直接报错（生产用） |
| `demo` | 完全不联网（CI / 演示用） |

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

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查、已分析条数、LLM 是否启用 |
| POST | `/v1/analyze?limit=10&force=false` | 跑一批：拉取 → 打标 → 抽取 → 入库（同步返回统计） |
| GET | `/v1/articles?tag=project_application&limit=20` | 已分析文章列表，可按标签过滤 |
| GET | `/v1/articles/{article_id}` | 单篇详情：标签 + 抽取结果 |
| GET | `/v1/tags` | 标签词表 + 每个标签的命中数 |
| GET | `/v1/tags/stats` | 标签分布与平均置信度 |
| GET | `/v1/schemas` | 已注册的抽取 schema |
| GET | `/v1/extractions?schema=&field=&due_before=` | 抽取结果平铺查询，`due_before` 按截止日期过滤 |
| GET | `/v1/runs` | 运行记录（模式、扫描/分析/跳过/错误数） |

CLI 也可：`docker compose run --rm insight python -m app analyze --limit 10`。

## 扩展

### 加标签

编辑 `app/vocab/tags.json`，往 `tags` 里加一条：`{ "id": "tender", "name": "招标采购", "group": "event", "keywords": ["招标","中标"] }`。

- `id` 是唯一契约，落库与查询都用它，**改名不要改 id**；
- `keywords` 只在无 LLM 的规则降级时使用，模型看的是 `id + name + group + keywords` 的完整描述；
- 几十个标签全部塞进单次 prompt 完全放得下，不需要两阶段召回。词表涨到几百条再考虑先粗筛候选。

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

1. **闭集打标，标签外一律丢弃。** 放任模型自造标签，会把 `Artificial Intelligence` 和 `AI` 变成两个标签——这是 [karakeep](https://github.com/karakeep-app/karakeep) issue #111 记录的原始问题，#529 的解法就是「只允许使用用户已建的标签」，并已合入。本项目在 `tagging.py` 里做了同样的守卫（词表外丢弃 + 告警）。标签集合与「标签→schema 路由」借鉴 [autolabel](https://github.com/refuel-ai/autolabel) 的闭集思路。
2. **打标与抽取分离。** 打标的输出空间是闭合的（必须是词表内 id），抽取的输出空间是固定的（必须是 schema 内字段）；打标漏标只是少一条线索，抽取编造会污染下游。因此两者用的是不同 prompt、不同校验：标签必须 `in` 词表，抽取值必须能在原文定位。
3. **相对时间以发布时间为基准。** 「自发布之日起30日内」「本月底」如果用抓取时间换算，会随重跑静默漂移。抽取提示词里显式传入 `published_at_utc`（缺失时回退 `published_at`），`normalize_deadline()` 按下这个基准推算。
4. **缺失字段返回 null，值要能在原文定位。** `_locate()` 做空白无关的子串校验，定位不到的值保留但标 `verified=false`，供人工复核，而不是静默采信。
5. **结果独立成库。** 见上文对照表。
6. **单篇失败不拖垮批次。** 单篇异常计入 `errors` 并继续，运行统计落到 `runs` 表。

## 数据表

| 表 | 内容 |
| --- | --- |
| `analyzed_articles` | 已分析文章元数据（`article_id` 主键，与上游对齐） |
| `article_tags` | `(article_id, tag)` 主键，含置信度、原文依据、引擎 |
| `extractions` | `(article_id, schema_id, field)` 主键，含值、原文片段、置信度、是否定位成功 |
| `runs` | 每次批处理的统计 |

## 测试

```bash
# 宿主机（需要 pip install -e ".[dev]"）
pytest -q

# 或在镜像里跑，不用本地装依赖
docker compose run --rm --entrypoint sh insight -c "pip install --user -q pytest && python -m pytest tests -q"
```

覆盖：日期归一（绝对/相对/跨年/月底/无基准）、闭集守卫（词表外丢弃、去重、max_tags）、demo 模式端到端（打标 + 申报抽取 + `due_before` 查询）、重跑幂等。

## 已知限制

- 抽取质量取决于模型与 prompt，本项目只保证「不越界」（不造标签、不造字段、值可定位），不保证召回；
- 相对截止时间只覆盖常见表述（`N日内`/`N个工作日`/`本月底`），「公示结束后5个工作日」这类链式表述仍会漏；
- `/v1/analyze` 同步执行，长批次需要外部调度；生产建议加锁或改成任务队列；
- 单进程单 SQLite，不适合多副本水平扩展；
- 演示数据与规则引擎只用于跑通链路，不作为效果基线。

## 致谢

架构取舍来自这几个项目的公开实践：[karakeep](https://github.com/karakeep-app/karakeep)（闭集标签）、[autolabel](https://github.com/refuel-ai/autolabel)（LLM 标注 + 校验）、[TrendRadar](https://github.com/sansan0/TrendRadar)（新闻流水线与推送形态）、[GeneralNewsExtractor](https://github.com/kingname/GeneralNewsExtractor)（上游正文抽取）。

## License

MIT，见 [LICENSE](LICENSE)。注意上游 news-crawler 为 GPL-3.0，本项目通过 HTTP 调用其 API，不链接其代码。
