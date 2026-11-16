# TVBoxKeeper · Code Wiki

> 适用版本：`tvboxkeeper 0.1.0`（`src/tvboxkeeper/__init__.py`）
> 生成日期：2026-11-16
> 配套文档：[产品文档.md](./产品文档.md)（需求与决策记录）、[README.md](../README.md)（上手操作）

本文档面向"要读代码/改代码的人"，回答四个问题：

1. 这个项目由哪些模块组成，各自负责什么；
2. 数据在模块之间怎么流动，关键类与函数在做什么；
3. 模块之间的依赖关系与边界约束；
4. 怎么把它跑起来（CLI / CI / 本地计划任务）。

---

## 目录

- [1. 项目概览](#1-项目概览)
- [2. 技术栈与目录结构](#2-技术栈与目录结构)
- [3. 架构总览](#3-架构总览)
- [4. 模块职责](#4-模块职责)
- [5. 数据模型](#5-数据模型)
- [6. 关键函数说明](#6-关键函数说明)
- [7. 核心流程](#7-核心流程)
- [8. 状态机与健康分](#8-状态机与健康分)
- [9. 配置项](#9-配置项)
- [10. 依赖关系](#10-依赖关系)
- [11. 发布产物](#11-发布产物)
- [12. 运行与部署](#12-运行与部署)
- [13. 测试](#13-测试)
- [14. 已知约束与扩展点](#14-已知约束与扩展点)

---

## 1. 项目概览

TVBoxKeeper 是一个**源维护工具**，不是播放器、不是导航站。它做四件事：

| 阶段 | 动作 | 产物 |
|---|---|---|
| 发现 | 人工种子、多仓递归展开、GitHub 搜索 | 源库中新增待体检的 URL |
| 体检 | L1 可达 + 解析、L2 子资源探测、L3 抽样真打 CMS 接口 | `check_log` 记录、状态、健康分 |
| 沉淀 | SQLite 源库 + gzip 内容快照 + 直播线路测活结论 | `data/tvbox.sqlite`、`data/snapshots/` |
| 发布 | 多仓订阅、直播合集、超级直播列表、报告、静态看板、镜像 | `output/*` |

### 1.1 设计边界（代码里被反复强调的红线）

1. **不执行任何源里的代码**：`spider` 指向的 jar 只做 HTTP 探测与 md5 比对，绝不加载。
2. **抓到的内容一律视为不可信数据**：只走 `json.loads` 级别的纯文本解析（`classify._extract_json`），没有 `eval`/`exec`/动态 import。
3. **只存元数据与结论**：快照存原始配置文本（gzip）供 diff 与镜像，不存任何音视频。
4. **对目标站点友好**：全局并发上限、单次超时、重试、随机 UA 全部可配（`settings.yaml`）。

### 1.2 一次完整跑批的流水线

```
import ──► discover ──► hunt ──► check ──► livetest ──► publish ──► notify
(种子)     (多仓子仓    (GitHub    (L1/L2/   (直播线路    (多仓/报告/    (Summary/
            +直播源)     挖源)      L3)       测活)        看板/镜像)      IM 推送)
```

在 GitHub Actions 中这条链由 [.github/workflows/daily-check.yml](../.github/workflows/daily-check.yml) 每日执行；本地可用 `scripts/daily-task.bat` 兜底（只跑 `import → check → publish`）。

---

## 2. 技术栈与目录结构

### 2.1 技术栈

| 项 | 选型 | 说明 |
|---|---|---|
| 语言 | Python ≥ 3.11 | `pyproject.toml` 的 `requires-python` |
| HTTP | `httpx` ≥ 0.27 | 体检/发现用 `AsyncClient`，GitHub 挖源/通知用同步 `Client`/`httpx.post` |
| CLI | `typer` ≥ 0.12 | 每个命令一个 `@app.command()` 函数 |
| 配置 | `pyyaml` ≥ 6 | `settings.yaml` / `sources.yaml` |
| 存储 | `sqlite3`（标准库） | 单文件库 `data/tvbox.sqlite`，表结构内嵌在 `registry._SCHEMA` |
| 测试 | `pytest` ≥ 8 | `pyproject.toml` 中 `testpaths=["tests"]`、`pythonpath=["src"]` |
| 前端 | 无框架、零外部资源 | 看板 HTML/CSS/JS 由 `publish.py` 以字符串常量内联生成 |

依赖刻意保持极小：没有 ORM、没有 Web 框架、没有调度库（调度交给 Actions / Windows 计划任务）。

### 2.2 目录结构

```
TVBox源/
├── pyproject.toml              # 打包与入口点：tvboxkeeper = tvboxkeeper.cli:app
├── README.md                   # 用户操作手册
├── .github/workflows/
│   └── daily-check.yml         # 每日 05:30(北京) 跑批 + Pages 部署 + 失败告警
├── config/
│   ├── settings.yaml           # 全局参数（覆盖 config.py:DEFAULTS）
│   └── sources.yaml            # 人工种子源清单
├── src/tvboxkeeper/
│   ├── __init__.py             # __version__
│   ├── __main__.py             # python -m tvboxkeeper 入口
│   ├── cli.py                  # Typer 命令层（唯一入口，编排其余模块）
│   ├── config.py               # 配置加载 + DEFAULTS 默认值
│   ├── models.py               # 数据类（无外部依赖）
│   ├── normalize.py            # URL 规范化 / source_id / 默认名称
│   ├── fetch.py                # 异步 HTTP 抓取（限速由调用方 Semaphore 控制）
│   ├── classify.py             # 配置文本解析 → SchemaInfo（纯 JSON，不执行）
│   ├── validate.py             # 体检核心：L1/L2/L3、增量跳过、内容特征校验
│   ├── registry.py             # SQLite 源库：状态流转、健康分、快照、统计
│   ├── compliance.py           # 合规关键词扫描（命中自动封禁）
│   ├── diffing.py              # 快照站点级 diff
│   ├── discover.py             # 多仓子仓展开 + 直播源提取
│   ├── github_hunt.py          # GitHub 仓库/代码搜索挖源
│   └── publish.py              # 所有发布产物生成（含内联 CSS/JS）
├── data/                       # 运行时数据（快照目录被 gitignore）
│   ├── tvbox.sqlite            # 源库
│   ├── snapshots/<sid>/*.json.gz
│   └── local-run.log           # 本地兜底任务日志
├── output/                     # 发布产物（提交回仓库，供订阅与 Pages）
│   ├── 多仓.json / 多仓-lite.json
│   ├── 直播.json / 直播-lite.json / super_live.txt
│   ├── 报告.md / 周报.md / last_diff.json
│   ├── mirrors/<sid>.json + index.json
│   └── site/index.html + site/source-<sid>.html
├── scripts/
│   ├── daily-task.bat          # 本地兜底批次（必须保持纯 ASCII）
│   └── install-task.ps1        # 注册 Windows 计划任务（每天 06:10）
└── tests/                      # 12 个测试文件，见第 13 章
```

---

## 3. 架构总览

### 3.1 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 命令层  cli.py（Typer）                                        │
│   init import add star ban check discover livetest hunt        │
│   history stats publish notify list                            │
└───────────────┬──────────────────────────────────────────────┘
                │ 只做参数解析、输出格式化、把模块串起来
┌───────────────▼──────────────────────────────────────────────┐
│ 能力层（可按需独立调用，均不依赖 cli）                          │
│  discover.expand   github_hunt.Hunter   validate.check_source  │
│  publish.build_*   registry.Registry                           │
└───────────────┬──────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│ 基础层（无业务状态，纯函数为主）                                 │
│  config  normalize  fetch  classify  models                     │
│  compliance  diffing                                           │
└──────────────────────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│ 外部：HTTP(S) 目标站 / GitHub API / SQLite 文件 / output 目录    │
└──────────────────────────────────────────────────────────────┘
```

关键约定：**依赖只向下**。`models.py` 不 import 任何项目模块；`classify.py` 只依赖 `models`；`registry.py` 依赖 `models/normalize/compliance/diffing`；`publish.py` 除 `classify/diffing/normalize` 外不碰数据库（行数据由 cli 传入），因此发布逻辑可以纯函数式测试。

### 3.2 端到端数据流（以 check → publish 为例）

```
             ┌───────────────┐
settings.yaml│  load_settings │──► settings(dict)，缺省回落 DEFAULTS
sources.yaml └───────┬───────┘
                     │ load_seeds
                     ▼
              ┌──────────────┐   upsert()    ┌──────────────────┐
              │   Registry   │◄─────────────►│ data/tvbox.sqlite│
              │              │   record()    │  source/check_log│
              │              │   snapshot    │  snapshot/origin │
              └──────┬───────┘               │  live_probe      │
                     │ rows                  └──────────────────┘
                     ▼
      ┌──────────────────────────────┐
      │ validate.check_source × N    │  ← httpx.AsyncClient + Semaphore
      │  L1 fetch → classify.parse   │
      │  L2 _probe → ProbeResult[]   │
      │  L3 _probe_site_api          │
      └──────────────┬───────────────┘
                     │ CheckOutcome
                     ▼
      ┌──────────────────────────────┐
      │ Registry.record()            │
      │  状态 / 健康分 / detail / 快照 │
      └──────────────┬───────────────┘
                     │ rows + snapshots
                     ▼
      ┌──────────────────────────────┐   ┌──────────────┐
      │ publish.build_*              │──►│ output/*     │
      └──────────────────────────────┘   └──────────────┘
```

---

## 4. 模块职责

| 模块 | 行数量级 | 职责 | 对外主要符号 |
|---|---|---|---|
| `__init__.py` | 3 | 版本号 | `__version__` |
| `__main__.py` | 4 | `python -m tvboxkeeper` 入口 | — |
| `cli.py` | ~790 | 命令定义、参数校验、控制台输出、把各模块串联 | `app`、`l3_targets()`、`_run_checks()` |
| `config.py` | ~140 | 默认配置字典 + 深合并加载 + 种子清单读取 | `DEFAULTS`、`load_settings()`、`load_seeds()` |
| `models.py` | ~60 | 三个核心数据类 | `ProbeResult`、`SchemaInfo`、`CheckOutcome` |
| `normalize.py` | ~42 | URL 收敛（去追踪参数/去重复协议头/去 fragment）、稳定 ID、默认名称 | `normalize_url()`、`source_id()`、`guess_name()` |
| `fetch.py` | ~89 | 异步 GET，Range 局部读取，失败退避重试，证书类错误识别 | `FetchResult`、`fetch()`、`is_cert_error()`、`make_headers()` |
| `classify.py` | ~280 | 把配置文本解析成 `SchemaInfo`：单仓/多仓/直播/反爬/空/无效，并给出 L2 探测清单 | `parse_config()`、`parse_live()`、`decode_content()`、`site_labels()` |
| `validate.py` | ~186 | 体检主流程：L1→L2→L3、增量跳过、内容特征校验、jar md5 校验 | `check_source()`、`probe_stream()`、`should_skip_probes()` |
| `registry.py` | ~640 | SQLite 全部读写：登记、状态流转、健康分、快照、统计聚合、去重、清理 | `Registry`、`reason_of()` |
| `compliance.py` | ~28 | 敏感关键词扫描 | `scan()`、`keywords_of()` |
| `diffing.py` | ~33 | 两份快照的站点集合差异 | `content_change()`、`describe()` |
| `discover.py` | ~107 | 展开多仓子仓 + 从快照提取直播源 | `expand()`、`HINT` |
| `github_hunt.py` | ~137 | GitHub 搜索仓库/代码 → 探测常见文件名 → 登记 | `Hunter` |
| `publish.py` | ~1900 | 多仓/直播/超级列表/报告/周报/看板/详情页/镜像/通知文本 | `build_duocang()`、`build_site()`、`build_super_live()` 等 |

### 4.1 各模块边界说明

- **cli.py 不写业务算法**：状态判定、评分、解析规则都在下层；cli 只负责取数、并发编排、打印。
- **publish.py 不读数据库**：所有行数据由 cli 查询后作为 `list[dict]` 传入，因此 `build_duocang`/`build_report`/`build_site` 都可以用假数据单测。
- **registry.py 是唯一写库者**：`validate`、`discover`、`github_hunt` 只产出结果对象或调用 `Registry` 的方法。
- **fetch.py 不管并发**：`Semaphore` 由调用方传入，便于按命令使用不同并发（体检 8、直播测活 6）。

---

## 5. 数据模型

### 5.1 运行期数据类（`models.py`）

#### `ProbeResult` —— 单个子资源的探测结论

| 字段 | 类型 | 含义 |
|---|---|---|
| `url` | `str` | 被探测的地址 |
| `kind` | `str` | `jar` / `parse` / `live` / `channel` / `wallpaper` |
| `status` | `int` | HTTP 状态码，0 表示网络层失败 |
| `latency_ms` | `int` | 本次请求耗时 |
| `error` | `str` | 错误摘要 |
| `content_ok` | `bool` | 内容特征是否像该类型（防 200 错误页） |
| `md5_ok` | `bool \| None` | jar 带声明 md5 时的校验结果，`None` 表示未校验 |

`ProbeResult.ok` 属性是判定入口：

```python
if not (200 <= self.status < 400): return False
if self.md5_ok is not None:        return self.md5_ok     # 声明了 md5 就以哈希为准
return self.content_ok
```

#### `SchemaInfo` —— 配置解析结果

| 字段 | 含义 |
|---|---|
| `schema_type` | `tvbox` / `storehouse` / `mixed` / `live` / `anti_crawl` / `empty` / `invalid` |
| `sites_count` / `parses_count` / `lives_count` / `storehouse_count` | 对应顶层数组的字典元素个数 |
| `live_channels` / `live_groups` | 直播文件的频道数与分组数 |
| `site_apis` | type 0/1 且 api 为 http(s) 的站点接口，L3 抽样用，最多 10 个 |
| `jar_md5` | `{jar_url: md5}`，来自 `spider: "url;md5;hash"` |
| `compat` | 分支适配标注：`通用` / `偏影视仓` / `需特定分支` |
| `probes` | `[(kind, url), ...]`，顺序即探测优先级 |
| `children` | 多仓子仓 `[{"sourceName","sourceUrl"}]` |

#### `CheckOutcome` —— 一次体检的完整结果

`url`、`ok`、`status`、`latency_ms`、`error`、`level`（实际执行的最高档位）、`schema`、`probes`、`l3_tested`/`l3_ok`、`tls_insecure`（是否降级为不校验证书）、`skipped_probes`（增量跳过）、`content`（原始字节，供快照）。

> `level` 记录的是**真实完成的探测动作**，不是配置档位：只解析成功记 `L1`，跑了子资源记 `L2`，跑了 CMS 接口记 `L3`。旧数据缺失时 `record()` 会按 `sub_total` 回退推断。

#### `FetchResult`（`fetch.py`）

`url`、`status`、`content`、`latency_ms`、`error`。与 `ProbeResult` 分开，是为了让抓取层对解析层零依赖。

### 5.2 SQLite 表结构（`registry._SCHEMA`）

```
source ────────────── 每个源一行（主键 id = sha1(normalized_url)[:16]）
  id url name gen schema_type status
  first_seen last_ok last_checked
  streak_ok streak_fail latency_ms
  sites_count parses_count lives_count storehouse_count
  sub_ok sub_total l3_tested l3_ok
  live_channels live_groups compat
  content_md5 dup_of health_score
  starred banned notes
  probe_ts            # 最近一次真正执行 L2/L3 的时间（增量体检依据）

check_log ─────────── 每次体检一行（时序数据，容量最大）
  rowid_seq source_id ts level ok latency_ms detail(JSON)

snapshot ──────────── 内容快照索引（文件在 data/snapshots/）
  source_id ts content_md5 gzip_path(相对路径)

origin ────────────── 来源渠道
  source_id channel ref ts    # manual|storehouse|lives|github|github_code|github_repo

live_probe ────────── 直播线路测活结论（url 维度，非源维度）
  url_id url ts ok latency_ms

索引：check_log(source_id)、check_log(ts)、snapshot(source_id)
```

`check_log.detail` 是 JSON 字符串，键位固定：

| 键 | 含义 |
|---|---|
| `http` / `schema` / `error` | 状态码、解析类型、错误摘要 |
| `subs` | 子资源摘要，如 `["jar:200", "live:404"]` |
| `l3` | `[抽测数, 通过数]` |
| `tls` | 1 表示本次按"不校验证书"抓取（事后审计用） |
| `skip` | 1 表示增量体检跳过了子资源探测 |
| `compliance` | 命中的合规关键词 |
| `chg` | 站点级变更 `{"a":新增数,"r":移除数,"add":[...],"del":[...]}` |

### 5.3 迁移与可移植性

- `Registry._migrate()` 用 `PRAGMA table_info(source)` 对比列名，缺列就 `ALTER TABLE ADD COLUMN`（新增 `l3_tested/l3_ok/live_channels/live_groups/compat/content_md5/dup_of/probe_ts`）。
- `_normalize_snapshot_paths()` 把历史绝对路径收敛为相对 `snapshots/` 的路径——**不能**用 `is_absolute()` 判断，因为 POSIX 路径（`/home/runner/...`）在 Windows 上会被判为相对路径。这样库可以跨机器/跨 CI 复用。

---

## 6. 关键函数说明

### 6.1 `config.py`

```python
DEFAULTS: dict[str, Any]
def _deep_merge(base: dict, extra: dict) -> dict   # 深拷贝 base 后递归合并，避免污染 DEFAULTS
def load_settings(path: Path | None) -> dict       # 读 yaml（缺失/为空都安全），回落 DEFAULTS
def load_seeds(path: Path) -> list[dict]           # 读 sources.sources，支持 dict 或纯 url 字符串
```

`_deep_merge` 用 `copy.deepcopy` 复制，是因为 `DEFAULTS` 是模块级常量：若不拷贝，调用方改 `settings["compliance"]["keywords"]` 会永久污染进程内默认值（`tests/test_config.py` 专门覆盖了这一点）。

### 6.2 `normalize.py`

| 函数 | 行为 |
|---|---|
| `normalize_url(url)` | 去掉首尾空白；把 `https://https://x` 压成单协议头；scheme 小写；剔除 `utm_*`、`spm`、`from`、`share_token` 等追踪参数；丢弃 fragment |
| `source_id(url)` | `sha1(normalize_url(url))` 前 16 位十六进制 —— 全库主键 |
| `guess_name(url)` | 去 `www.` 的主机名（去掉 userinfo 与端口），作为未命名源的默认名 |

规范化是**去重的前提**：同一个源带不同 utm 参数会被视为同一行（`source.url` 有 UNIQUE 约束）。

### 6.3 `fetch.py`

```python
async def fetch(client, sem, url, *, max_bytes, timeout, retries, probe_bytes=0) -> FetchResult
```

- `probe_bytes > 0` 时带 `Range: bytes=0-{n-1}` 只取前 N 字节（子资源探测用），同时始终用 `max_bytes` 截断读取量。
- 重试 `retries + 1` 次，退避 `min(1.0 * attempt, 3.0)` 秒；延迟只统计当前请求，不含重试等待。
- 捕获 `httpx.HTTPError / OSError / asyncio.TimeoutError`，返回 `status=0` + `error` 文本。

`is_cert_error(error)` 只匹配"证书链校验失败"这一类可安全降级的错误（`CERT_VERIFY_HINTS`）。**刻意不含** handshake failure / 协议版本不符——关掉校验也连不上，降级只会掩盖真实原因。

### 6.4 `classify.py`

解析链：`decode_content` → `_extract_json` → `parse_config`。

| 函数 | 关键点 |
|---|---|
| `decode_content(bytes)` | 依次尝试 `utf-8-sig` / `utf-8` / `gb18030`，最后 `errors="replace"` 兜底（国内源常见 GBK） |
| `_extract_json(text)` | 先 `json.loads`；失败则用 `JSONDecoder.raw_decode` 从每个 `{`/`[` 起点重试——兼容 BOM、前后杂文本、JSONP 包裹 |
| `parse_config(url, text)` | 主入口，返回 `SchemaInfo`；解析失败时按 HTML→`anti_crawl`、直播文本→`live`、空白→`empty`、其余→`invalid` 分类 |
| `parse_live(text)` | 统计 `#genre#` 分组数、频道数，抽前 5 条地址作为 L2 `channel` 探测样本 |
| `site_labels(text)` | 提取站点标识（`key` → `name` → `api`），只给 diff 用，不参与评分 |

`parse_config` 内部还做三件事：

1. **多仓识别**：同时支持 `storeHouse[{sourceName,sourceUrl}]` 与顶层 `urls[{url,name}]` 两种主流格式。
2. **分支适配启发式**（`_compat_tag`）：`drpy`/私有 type 占比 > 30%/10% → `需特定分支`；`csp_` 占比 > 30% → `偏影视仓`；否则 `通用`；多仓无站点则不标注。
3. **探测清单构造**：`spider`（含 md5）→ `parses[].url` → `lives[].url`（支持 `;` 分隔多地址）→ `wallpaper`，相对路径用 `urljoin` 补全，`proxy://` 一律跳过。

### 6.5 `validate.py`

```python
async def check_source(client, sem, url, settings, level="", prev=None) -> CheckOutcome
```

执行顺序：

1. **L1**：`fetch` 全量抓取（`network.max_bytes/timeout/retries`）。`status==0` 或 `>=400` 直接失败返回。
2. **解析**：`classify.decode_content` + `parse_config`；`invalid/anti_crawl/empty` 三类给出人话错误并返回（`ok=False`）。
3. 否则 `ok=True`；`level` 取参数覆盖或 `settings["validate"]["level"]`。
4. **增量**：`L2` 档位下若 `should_skip_probes(prev, content, settings)` 为真，置 `skipped_probes=True` 直接返回（**L3 永不跳过**，它是抽样的播放链路验证）。
5. **L2**：`probes[:max_probes]` 并发探测，`level="L2"`。
6. **L3**：`site_apis[:l3.sample_sites]` 打 `?ac=list`，记录 `l3_tested/l3_ok`，`level="L3"`。

辅助函数：

| 函数 | 作用 |
|---|---|
| `should_skip_probes(prev, content, settings)` | 内容 md5 未变 **且** `probe_ts` 在 `probe_every_days` 窗口内才跳过；窗口外强制重探（jar/直播地址可能已挂） |
| `_content_ok(kind, body, status)` | 内容特征校验：`jar`→`PK` 魔数；`live`→含 `#genre#`/`#extm3u`/`http`/`rtsp`；`channel`/`parse`→非 HTML 错误页；`wallpaper` 只看状态 |
| `_probe(...)` | 单子资源探测；`kind=="jar"` 且带 md5 时**全量下载**（上限 `jar_max_bytes`）做哈希比对 |
| `probe_stream(...)` | 直播线路测活：4KB 局部读 + `channel` 特征判定，供 `livetest` 使用 |
| `_probe_site_api(...)` | L3 单站点接口探测：返回体含 `"list"`/`"code"`/`<list` 视为通过 |

### 6.6 `registry.py`

#### 写入类方法

| 方法 | 说明 |
|---|---|
| `upsert(url, name, gen, origin, ref, starred, banned)` | `INSERT OR IGNORE` + 可选字段更新，返回 `(source_id, 是否新建)`；每次调用都写一条 `origin` 审计 |
| `record(outcome, settings)` | 体检结果落库：合规扫描 → 状态流转 → `check_log` → 健康分 → 快照（详见 7.1） |
| `set_flag(sid, field, value)` | 只允许 `starred / banned / notes / name`；封禁时同步把 `status` 置 `banned` |
| `note_hunt_batch(names)` / `hunted_recently(full, days)` | GitHub 挖源去重：`origin` 表里 `channel='github_repo'` 的最近记录 |
| `record_live_probe(url, ok, latency_ms)` | 直播线路结论（`url_id = source_id(url)`，`INSERT OR REPLACE` 覆盖最新一次） |
| `_save_snapshot(sid, content, keep)` | gzip 写入 `snapshots/<sid>/<ns>.json.gz`，库内存相对路径，滚动保留 `keep` 份 |
| `cleanup(settings)` | 按 `cleanup.*_days` 删除 `check_log/origin/live_probe/snapshot`（含物理文件）并 `VACUUM` |
| `dedupe()` | 每次全量重算：内容 md5 相同且 `active` 的源按 `(starred, health_score)` 排序，非最优的标 `dup_of` |

#### 查询与聚合类方法

| 方法 | 返回 |
|---|---|
| `sources(status="", include_banned=False)` | 按 `health_score DESC` 排序的行列表 |
| `history(sid, limit)` | 时间正序的体检日志，`detail` 已反序列化到 `parsed` |
| `availability(sid, days=30)` | 近 N 天成功率（%），无记录返回 `None` |
| `daily_stats(days)` | 按天聚合的 `[{date,total,ok,rate}]`，看板趋势折线用 |
| `host_failures(days, min_fails, limit)` | 失败按主机聚合，用于区分"整站挂了"与"单源失效" |
| `recent_changes(hours, limit)` | 最近内容发生站点级变化的源（每源取最新一条） |
| `failure_reasons(days)` | 失败原因归一后计数（`reason_of` 做归类） |
| `fresh_live_probes(days)` | `{url: (ok, ts)}`，超级直播列表剔除死线的依据 |
| `latest_snapshot(sid)` | `(content, ts)` 或 `None`（读不到文件时安全返回 `None`） |

`reason_of(detail)` 把错误文本归一成可统计的原因：`HTTP 4xx/5xx`、`空响应/疑似反爬`、`DNS 解析失败`、`超时`、`连接失败/被重置`、`SSL 证书错误`、`结构无效/反爬页`、`HTML 反爬页（疑似存活）`。

### 6.7 `compliance.py`

```python
def keywords_of(settings) -> list[str]   # compliance.enabled=False 时返回 []
def scan(content: bytes, settings) -> str | None   # 返回命中的第一个关键词
```

匹配策略刻意保守：只收具体品牌词（`bet365`、`太阳城`、`麻豆传媒`…），不做泛化猜测，避免误伤正常站点名。命中后 `record()` 会把 `ok` 强制改为 `False` 并自动 `banned=1`。

### 6.8 `diffing.py`

```python
def content_change(old: bytes, new: bytes, limit=6) -> dict
def describe(change: dict) -> str
```

以 `site_labels` 的集合差计算新增/移除；无变化返回 `{}`（因此 `record` 只在 md5 变化时才调用它）。直播配置没有 `sites` 字段，恒返回空——它们的变更由 md5 体现。

### 6.9 `discover.py`

```python
async def expand(registry, client, sem, settings) -> tuple[int, int]   # (新子仓数, 新直播数)
```

- **父仓筛选**：`gen < discover.max_depth` 且 `schema_type in (storehouse, mixed)` 且状态 `active/flaky/new`。
- **取文本优先级**：先读 `latest_snapshot`（多仓父源体积大，快照变化慢），无快照才联网。
- **子仓登记**：`gen = 父.gen + 1`、`origin="storehouse"`、`ref=父 url`；只接受 `http(s)`。
- **直播源提取**：遍历 `tvbox/mixed` 且 `active/flaky` 的源，从快照解析出 `lives[].url`，登记为独立一等源（`origin="lives"`，名字加 `·直播` 后缀）。
- 两个 `max_new_per_run` / `max_live_per_run` 上限防止单轮爆炸。

### 6.10 `github_hunt.py`

`Hunter`（同步 `httpx.Client`，因为 GitHub Search 限流场景不需要高并发）：

| 方法 | 说明 |
|---|---|
| `_search(path, params)` | 403/429 → 记录限流提示并返回 `None`（调用方据此停止后续搜索） |
| `_raw_base()` | `raw.githubusercontent.com` 前缀，可被 `github.raw_mirror` 加代理前缀（国内直连常不通） |
| `search_repos()` | 按 `repo_queries` 逐个搜仓库，查询之间 `time.sleep(search_interval)`（无 token 时 Search API 10 次/分钟） |
| `code_candidates()` | 需 token；把 `github.com/o/r/blob/b/p` 改写成 raw 地址 |
| `_try_url(url)` | 计数受 `max_fetches_per_run` 限制；只有 `schema_type in (tvbox, storehouse, mixed)` 才算命中 |
| `hunt()` | 仓库 → 常见文件名（`config.json`、`多仓.json`…）逐个探测 → 登记；随后处理代码搜索结果；落 `hunted_recently` 去重记录 |

### 6.11 `publish.py`

按用途分组（全部为纯函数，输入 `list[dict]`）：

#### 筛选与订阅

| 函数 | 规则 |
|---|---|
| `pick_sources(rows, min_health)` | `active` + 健康分达标 + 未封禁 + 非 `dup_of` + 非直播；排序 `(-starred, -health_score)` |
| `pick_live_sources(rows, min_health)` | `schema_type=="live"` + `active` + 达标；排序 `(-starred, -live_channels)` |
| `build_duocang(rows, min_health, mirror_base, mirrored_sids, limit)` | 生成 `{"storeHouse":[{sourceName, sourceUrl, sourceDelay}]}`；有镜像的源改用镜像地址；`limit>0` 取前 N（精简版） |
| `build_live_config(...)` | 生成 `{"lives":[{name,type:0,url}]}` |

#### 报告与文本

| 函数 | 说明 |
|---|---|
| `build_report(...)` | Markdown 报告：状态分布、本轮 diff、内容变化、推荐订阅、失败原因、失败主机、归并组、全量榜单表 |
| `build_weekly_markdown(...)` | 周日周报：状态分布、7 天可用率、内容变化、失败原因/主机 |
| `format_diff_text(diff)` | `last_diff.json` → 通知用 Markdown |
| `failure_text()` | 跑批失败告警文本（含 Actions 运行链接） |
| `should_notify(diff, weekly_fresh, settings)` | 无变化且无新周报时静默（`notify.silent_when_unchanged`） |
| `webhook_payload(platform, text)` | 飞书 `msg_type/content`、钉钉/企微 `msgtype/text` |
| `dingtalk_sign(url, secret)` | HMAC-SHA256(timestamp + "\n" + secret) 加签 |

#### 看板与页面

| 函数 | 说明 |
|---|---|
| `build_site(rows, histories, ...)` | 自包含目录页：状态卡片、订阅地址卡片、趋势面板、可排序/可筛选/可分页表格、失败主机与原因表 |
| `build_source_detail(row, history)` | 单源详情页（元信息 + 体检历史 + 内容变更） |
| `_th(key, label, kind, cls)` | 可排序表头（`data-sort/data-type` + 真实 `<button>`，键盘与读屏可用） |
| `_sparkline(marks)` | 最近 N 次体检的内联 SVG 折线 |
| `_trend_figure(points, ...)` | 趋势面板：**SVG 只画几何**（`viewBox` + `preserveAspectRatio="none"`，配 `non-scaling-stroke`），轴标签/数值/tooltip 全用 HTML 百分比定位，避免被非等比拉伸 |
| `_trend_panel(points, ...)` | 最近一周 / 最近一月 / 全部 三视图；全部视图超 60 天按 ISO 周聚合，超 365 天不展示 |
| `_weekly_aggregate(points)` | 按 ISO 周合并 `total/ok` 并重算 `rate` |
| `channel_key(name)` | 频道名归一：`中央/央视/央視 → CCTV`、`N套 → N`、`+ → P`，去掉清晰度/频道描述词与分隔符 |
| `_parse_live_channels(text)` | 完整解析 txt/m3u → `[(分组, 频道名, url)]` |
| `build_super_live(sources, max_urls=4, dead_urls)` | 多源合并：同名频道一行、多线路 `#` 互备（每频道最多 4 条）、剔除测活死线、分组按 央视 → 卫视 → 其他 排序 |

### 6.12 `cli.py`

| 函数/命令 | 说明 |
|---|---|
| `_root()` | 优先 `TVBOX_ROOT` 环境变量，否则当前工作目录 |
| `_open()` | 返回 `(root, settings, Registry)`，保证 `data/` 目录自动创建 |
| `_fmt(name, width)` | 中文按 2 列计算显示宽度，控制台表格对齐 |
| `l3_targets(rows, settings)` | 每日 L3 抽样名单：`active` + 未封禁 + 非重复 + 非直播 + 健康分 ≥ `min_health`，取前 `daily_quota` 个 |
| `_run_checks(reg, settings, rows)` | 体检编排：UA 随机、Semaphore、`prev_map`、L3 名单、TLS 降级复检、逐条 `record` |
| `init / import / add / star / ban` | 初始化与人工维护 |
| `check` | 全量（或 `--limit/--only`）体检 |
| `discover / hunt / livetest` | 三个采集命令 |
| `history / stats / list` | 查询与终端报表 |
| `publish` | 全部产物生成（见 7.2） |
| `notify` | Summary + IM 推送，`--event failed` 为失败告警 |

---

## 7. 核心流程

### 7.1 体检落库（`Registry.record`）

```
CheckOutcome
   │
   ├─(1) 源不存在则 upsert
   ├─(2) compliance.scan(content) ──命中──► ok=False, error="合规黑名单命中…"
   ├─(3) streak_ok / streak_fail 更新
   ├─(4) 状态判定（顺序敏感）
   │       hit 或已 banned ─────────────► banned
   │       ok ──────────────────────────► active
   │       schema in (invalid, empty) 且 http>0 ► invalid
   │       schema == anti_crawl ───────► dead（超过 grace 次）否则 anti_crawl
   │       streak_fail >= 判死线 ──────► dead
   │       其余 ────────────────────────► flaky
   ├─(5) sub_ok / sub_total、level 回退推断、content_md5
   ├─(6) 若 md5 变化：读上一份快照 → diffing.content_change → chg
   ├─(7) 组装 detail JSON（http/schema/error/subs/l3/tls/skip/compliance/chg）
   ├─(8) INSERT check_log
   ├─(9) health_score = _score(...)
   ├─(10) UPDATE source（含 dup_of='' 重置、probe_ts 仅在真探测时推进）
   ├─(11) 命中合规 → banned=1 + notes
   └─(12) ok 且有内容 → _save_snapshot()
```

两个容易踩的细节：

- `dup_of` 每次体检都清空，去重结果由 `dedupe()` 在全量发布时统一重算——这样内容分叉后归并会自动解除。
- `probe_ts` 只在"真的跑了探测"（`sub_total` 或 `l3_tested` 非零）时前移，否则增量窗口永远滑不到头。

### 7.2 发布（`cli.publish` 的 12 步）

```
1. dedupe()                          同内容归并先落库，后面取到的 rows 才带 dup_of
2. rows = sources(include_banned=True) + 逐源 availability → r["avail"]
3. config_pick / live_pick           按门槛筛订阅候选
4. 镜像（mirror_base 非空时）
     ├─ 订阅内每个源读 latest_snapshot → write_mirror(output/mirrors/<id>.json)
     ├─ 读不到快照但上一轮有清单 → 沿用（kept 计数），不删可用镜像
     ├─ 至少镜像到 1 个源才清理陈旧镜像文件
     └─ 写 index.json，并按"配置 X/Y、直播 X/Y"分别统计比例
5. 多仓：build_duocang → 与上次发布比较得到 added/removed → 写 多仓.json
6. 直播合集：build_live_config（含镜像替换）
7. 超级直播列表
     ├─ 取每个 live_pick 的最新快照文本
     ├─ dead_urls = fresh_live_probes(窗口内 ok=0)
     ├─ build_super_live 合并 + 剔死线 → 写 super_live.txt
     └─ 若 mirror_base 可用，把超级列表地址插到 lives[0]
8. 写 直播.json
9. 精简版：build_duocang/build_live_config 带 limit → 多仓-lite.json / 直播-lite.json
10. 报告：recent_changes / host_failures / daily_stats(365 天) / 增量子资源统计
        → build_report → 报告.md
11. last_diff.json（notify 读取）；订阅地址卡片（由 mirror_base 推导）
12. 静态看板：每个非封禁源生成 source-<id>.html，再 build_site → site/index.html
        周日（weekday()==6）额外 build_weekly_markdown → 周报.md
        最后 reg.cleanup(settings) 清过期数据 + VACUUM
```

注意第 4 步的"沿用旧镜像"是一个**刻意的容错**：换机器或快照目录未随库同步时，读不到本地快照不应导致线上镜像被清空、订阅地址被迫回退到原站。

### 7.3 直播测活（`livetest`）

```
live_pick → 逐源 latest_snapshot → _parse_live_channels → url 去重
   │
   ├─ force          → 全部重测
   ├─ 默认            → 未测过的优先 + 已过期按 ts 最旧优先
   └─ cap = max_per_run（默认 1500）截断
   │
   ├─ 每 100 条一批 asyncio.gather(probe_stream)
   ├─ 每条 record_live_probe(url, ok, latency)
   └─ 打印累计通过率
```

免费直播流死亡率极高（文档记录首轮通过率约 10%），所以超级列表的构建必须依赖这份测活结论，否则榜单可信度不成立。

### 7.4 TLS 降级复检

```
第一批：verify=True 抓全部
   │
   ├─ is_cert_error(o.error) 的源 → 收集
   ├─ 若 verify_ssl_fallback 且有待复检源 → 用 verify=False 的 client 重抓这一批
   ├─ 复检结果替换原结果，并置 tls_insecure=True（写进 detail.tls 供审计）
   └─ 超时/DNS/握手失败不降级——关校验也连不上，降级只会掩盖真实原因
```

---

## 8. 状态机与健康分

### 8.1 状态流转

```
                  ┌────────── new（刚登记，未体检）
                  │
       体检通过   ▼
      ┌──────── active ◄─────────── 连续通过
      │             │
      │ 失败 1..N-1 ▼
      │           flaky
      │             │ 连续失败 >= dead_after_failures
      │             ▼
      │           dead ──── 恢复通过 ──► active
      │
      ├─ 可达但解析不出结构 ──► invalid
      ├─ 返回 HTML 反爬页 ────► anti_crawl（判死线放宽 anti_crawl_grace 倍）
      └─ 合规命中 / 人工封禁 ─► banned（不体检、不发布、不出现在看板表格）
```

| 状态 | 语义 | 是否参与发布 |
|---|---|---|
| `new` | 刚登记未体检 | 否 |
| `active` | 最近一次体检通过 | 是（还需分数达标） |
| `flaky` | 失败中但未达判死线 | 否 |
| `anti_crawl` | HTML 反爬页（可能仍存活） | 否 |
| `invalid` | 可达但解析不出 TVBox 结构 | 否 |
| `dead` | 连续 N 次失败 | 否 |
| `banned` | 人工封禁或合规命中 | 否 |

### 8.2 健康分公式

```
score = 100 × ( w_success × rate_recent10
              + w_subs    × sub_ok/sub_total     （无探针时按 1.0）
              + w_richness × richness )

richness = 多仓/混合：min(1, storehouse_count / 10)
           纯直播    ：min(1, live_channels / 300)
           单仓      ：min(1, sites_count/40 × 0.7 + lives_count/5 × 0.3)

本次体检失败 → score = min(score, 20)
权重默认 0.5 / 0.3 / 0.2（settings.score.weights）
```

设计意图：**稳定性优先**（近 10 次成功率占一半），其次是子资源可用性（jar/直播挂了但配置可达，分不会太高），内容丰富度只占两成。

---

## 9. 配置项

`config/settings.yaml` 与 `config.py:DEFAULTS` 字段一一对应，yaml 只写想覆盖的部分，其余自动回落默认值。

> 注意 `_deep_merge` 对**列表是整体替换**而不是追加：例如 `compliance.keywords` 在 yaml 里写了几项，就只剩这几项，不会与 `DEFAULTS` 里的默认词合并。

### 9.1 `network`

| 键 | 默认 | 说明 |
|---|---|---|
| `timeout` | 10 | 单次请求超时（秒） |
| `retries` | 2 | 失败重试次数 |
| `concurrency` | 8 | 全局并发上限（Semaphore） |
| `max_bytes` | 5242880 | 配置文件大小上限 5MB |
| `verify_ssl` | true | 是否校验证书 |
| `verify_ssl_fallback` | true | 证书链失败时是否对该源降级复检 |
| `user_agents` | 两个 UA | 每次跑批随机选一个 |

### 9.2 `validate`

| 键 | 默认 | 说明 |
|---|---|---|
| `level` | L2 | 默认档位 `L1`/`L2`（L3 由抽样名单触发） |
| `dead_after_failures` | 3 | 连续失败判死线 |
| `anti_crawl_grace` | 3 | 反爬页宽限倍数（3×3=9 次才判死） |
| `max_probes` | 12 | L2 单源最多探测子资源数 |
| `probe_timeout` | 8 | 子资源探测超时 |
| `jar_max_bytes` | 20971520 | jar 全量校验的下载上限 |
| `l3.sample_sites` | 3 | 每源抽测的 CMS 接口数 |
| `l3.enabled` / `daily_quota` / `min_health` | true / 5 / 85 | 每日 L3 抽样开关、名额、分数门槛 |
| `incremental.enabled` / `probe_every_days` | true / 3 | 增量体检 |

### 9.3 其余分组

| 分组 | 关键键 | 作用 |
|---|---|---|
| `score.weights` | `success/subs/richness` | 健康分权重（见 8.2） |
| `publish` | `min_health`、各产物路径、`lite.*`、`mirror_base` | 发布门槛与落盘位置 |
| `snapshot.keep` | 10 | 每源保留快照份数 |
| `livetest` | `concurrency/timeout/probe_every_days/max_per_run` | 直播线路测活 |
| `notify` | `silent_when_unchanged` | 无变化时是否静默 |
| `compliance` | `enabled/keywords` | 合规黑名单 |
| `cleanup` | `check_log_days/snapshot_days/origin_days/live_probe_days` | 数据保留期 |
| `discover` | `max_depth/max_new_per_run/max_live_per_run` | 多仓递归与增量上限 |
| `github` | `token_env/repo_queries/code_query/candidate_files/max_*/raw_mirror/search_interval/rehunt_days` | 挖源参数 |

`config/sources.yaml` 只有一组 `sources`，每项支持 `url`（必填）、`name`、`starred`、`banned`，也允许直接写 url 字符串。

---

## 10. 依赖关系

### 10.1 模块依赖图

```
                         ┌──────────┐
                         │  models  │   （零项目内依赖）
                         └────┬─────┘
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        ┌──────────┐    ┌──────────┐    ┌──────────┐
        │ classify │    │  fetch   │    │ normalize│
        └────┬─────┘    └────┬─────┘    └────┬─────┘
             │               │               │
   ┌─────────┼───────────────┼───────────────┼──────────┐
   ▼         ▼               ▼               ▼          ▼
┌────────┐ ┌────────┐  ┌───────────┐  ┌──────────┐ ┌─────────┐
│validate│ │github_ │  │ discover  │  │ registry │ │publish  │
│        │ │ hunt   │  │           │  │          │ │         │
└───┬────┘ └───┬────┘  └─────┬─────┘  └────┬─────┘ └────┬────┘
    │          │             │             │            │
    │          └─────────────┴─────────────┘            │
    │                        │                          │
    │                   ┌────▼─────┐   ┌──────────┐     │
    │                   │compliance│   │ diffing  │◄────┘
    │                   └────┬─────┘   └────┬─────┘
    │                        └──────────────┘
    └──────────────────────────┬─────────────────────────┘
                               ▼
                          ┌─────────┐
                          │  cli.py │  （唯一编排者）
                          └────┬────┘
                               ▼
                          ┌─────────┐
                          │ config  │
                          └─────────┘
```

（箭头方向 = "被依赖"；cli 位于最上层，`Registry`、`validate`、`discover`、`Hunter`、`publish` 互不直接依赖，全部通过 cli 串联。）

### 10.2 依赖规则速查

| 模块 | 允许依赖 | 禁止 |
|---|---|---|
| `models` | 仅标准库 | 任何项目模块 |
| `classify` | `models` | 网络、数据库 |
| `fetch` | 仅 `httpx` | 业务模块 |
| `normalize` | 标准库 | 业务模块 |
| `compliance` / `diffing` | `classify` | 数据库、网络 |
| `validate` | `classify`、`fetch`、`models` | 数据库、`registry` |
| `registry` | `models`、`normalize`、`compliance`、`diffing` | 网络、`cli`、`publish` |
| `discover` | `classify`、`fetch`、`normalize`、`registry` | `cli` |
| `github_hunt` | `classify`、`models`、`normalize`（registry 以参数注入） | `cli` |
| `publish` | `classify`、`diffing`、`normalize` | 数据库、网络 |
| `cli` | 全部 | — |

### 10.3 外部依赖与运行环境

| 依赖 | 用途 | 缺失时的影响 |
|---|---|---|
| `httpx` | 全部 HTTP | 无法运行 |
| `typer` | CLI | 无法运行 |
| `pyyaml` | 配置 | 无法运行 |
| `pytest`（dev） | 测试 | 仅测试不可用 |
| SQLite | 标准库 | — |
| `GITHUB_TOKEN`（env，可选） | 代码搜索 + 提高限流配额 | 挖源能力下降 |
| `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`、`FEISHU_WEBHOOK_URL`、`DINGTALK_WEBHOOK_URL`/`DINGTALK_SECRET`、`WECOM_WEBHOOK_URL`（env，可选） | 通知 | 只写 Actions Summary |
| `TVBOX_ROOT`（env，可选） | 指定数据根目录 | 默认当前工作目录 |

---

## 11. 发布产物

| 文件 | 生成函数 | 内容 |
|---|---|---|
| `output/多仓.json` | `build_duocang` | `storeHouse[{sourceName, sourceUrl, sourceDelay}]`，可直接作为 TVBox 配置地址 |
| `output/多仓-lite.json` | `build_duocang(limit=N)` | 门槛更高、条数更少（默认 ≥80 分、前 8 个） |
| `output/直播.json` | `build_live_config` | 只含 `lives` 的合集配置，可作配置地址导入 |
| `output/直播-lite.json` | `build_live_config(limit=N)` | 精简直播合集（默认 5 个） |
| `output/super_live.txt` | `build_super_live` | 多源频道归一合并的超级直播列表，同频道多线路 `#` 互备 |
| `output/报告.md` | `build_report` | 状态分布、diff、内容变化、订阅推荐、失败原因/主机、归并组、全量榜单 |
| `output/周报.md` | `build_weekly_markdown` | 每周日生成，随 `notify` 推送 |
| `output/last_diff.json` | `cli.publish` | 本轮 added/removed，`notify` 读取 |
| `output/mirrors/<sid>.json` | `write_mirror` | 该源最后一次成功抓取的原样内容（镜像订阅用） |
| `output/mirrors/index.json` | `cli.publish` | `{sid: {name, url, updated}}` 映射 |
| `output/site/index.html` | `build_site` | 自包含看板：状态卡片、订阅卡片、趋势面板、排序/筛选/分页表格、失败主机与原因 |
| `output/site/source-<sid>.html` | `build_source_detail` | 单源详情：元信息、内容变更、体检历史 |

看板特性（全部无外部资源，可直接挂 GitHub Pages）：亮/暗主题切换（`localStorage` 记忆）、多列叠加排序（Shift 或"叠加排序"开关）、每页条数记忆、URL 一键复制（含非 HTTPS 降级方案）、趋势图三档时间范围、`prefers-reduced-motion` 适配、窄屏列按优先级逐级隐藏。

---

## 12. 运行与部署

### 12.1 安装

```bash
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
pip install -e ".[dev]"
```

入口点由 `pyproject.toml` 注册：`tvboxkeeper = "tvboxkeeper.cli:app"`，同时也支持 `python -m tvboxkeeper`。

### 12.2 命令清单

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `init` | 建 `data/`、`output/`、SQLite 表 | — |
| `import` | 把 `config/sources.yaml` 登记进库 | `--file` |
| `add <url>` | 手工加一个源 | `--name`、`--starred` |
| `star <url或id>` | 置顶/取消 | `--off` |
| `ban <url或id>` | 封禁/解封 | `--off` |
| `check` | 全量体检 | `--level L1/L2`、`--limit N`、`--only 关键字` |
| `discover` | 展开多仓子仓 + 提取直播源 | — |
| `hunt` | GitHub 挖源 | 依赖 `GITHUB_TOKEN`（可选） |
| `livetest` | 直播线路全量测活 | `--force`、`--max N` |
| `publish` | 生成全部产物 | — |
| `notify` | 推送变化/失败告警 | `--event updated\|failed` |
| `history <url或id>` | 单源历史 + 30 天可用率 | `--limit N` |
| `stats` | 状态分布、失败原因、低可用源 | — |
| `list` | 查看源库 | `--status`、`--all` |

### 12.3 无人值守

**GitHub Actions**（`.github/workflows/daily-check.yml`）

- 触发：`cron: "30 21 * * *"`（UTC）= 北京时间每天 05:30；也可 `workflow_dispatch` 手动跑。
- `run` 作业：`import → discover → hunt → check → livetest → publish → notify`，随后提交产物与源库（`chore: 每日源体检更新 [skip ci]`）。
- 失败时执行 `notify --event failed`，向已配置渠道告警。
- `pages` 作业：先探测仓库 Pages 是否以 `workflow` 为发布源，未开通则告警跳过（不判定流水线失败），开通后部署 `output/site`。

**Windows 本地兜底**（`scripts/install-task.ps1`）

- 注册计划任务 `TVBoxKeeper-daily`，每天 06:10，`-StartWhenAvailable`（错过开机补跑）。
- `scripts/daily-task.bat` 只跑 `import → check → publish`（`discover/hunt` 交给 Actions）。
- 跑完 best-effort 回推：`git pull --rebase` 失败则 `rebase --abort` 并放弃推送，**绝不覆盖远端**；日志在 `data/local-run.log`。
- 两个脚本**必须保持纯 ASCII**：cmd 按 OEM 代码页解析 `.bat`、Windows PowerShell 5 误读无 BOM 的 UTF-8，写中文会破坏路径。

### 12.4 订阅抗失效（镜像）

配置 `publish.mirror_base` 指向本仓库 `output/mirrors/` 的 raw 地址后，发布时：

- 每个订阅源最后一次成功的内容快照写入 `output/mirrors/<id>.json`；
- 多仓/直播条目改用镜像地址，原站失效时订阅依然可用（内容为最后一次成功快照）；
- 源与原站重新一致时无需处理，下一轮快照会被覆盖。

---

## 13. 测试

`pytest` 配置：`testpaths=["tests"]`、`pythonpath=["src"]`，共 12 个测试文件、约 130 个用例。

| 测试文件 | 覆盖对象 | 代表用例 |
|---|---|---|
| `test_classify.py` | 解析器全分支 | 单仓/多仓/`urls` 格式/混合/直播 txt/m3u/反爬/空响应/JSONP+BOM/GBK/适配标注 |
| `test_validate.py` | L1/L2/L3 与内容特征 | jar 魔数、直播特征、parse 非 HTML、jar md5、L3 抽样与失败计数、档位回退、增量跳过窗口 |
| `test_tls_fallback.py` | TLS 降级 | 仅证书类错误降级、握手失败不降级、`verify_ssl=false` 行为、`tls_insecure` 标记 |
| `test_registry.py` | 源库全部行为 | 状态流转、评分区间、快照滚动与相对路径迁移、反爬宽限、去重与分叉解除、清理、日志索引、`probe_ts` 推进条件 |
| `test_discover.py` | 多仓展开 | 优先复用快照不联网、无快照时联网、死父源跳过 |
| `test_github_hunt.py` | 挖源 | 有效配置登记、近期仓库跳过、无效文件忽略、限流处理 |
| `test_publish.py` | 发布产物（体量最大） | 多仓筛选/命名/延迟/镜像/精简、超级直播合并与死线剔除、频道名归一、看板排序与分页、趋势图各种边界（单点/全平/缺日/轴放大/刻度拥挤）、主题切换、详情页、周报、钉钉签名、webhook 体 |
| `test_cli_publish.py` | 发布命令级行为 | 归并组进报告、镜像比例分组、快照读不到时保留镜像、精简订阅落盘、静默策略、失败告警 |
| `test_config.py` | 配置隔离 | 深拷贝不污染 `DEFAULTS`、yaml 覆盖不泄漏 |
| `test_compliance.py` | 合规 | 命中/未命中、关闭开关、自定义关键词、自动封禁 |
| `test_diffing.py` | 内容 diff | 站点增删、无变化、名称回退、直播文本无 diff |
| `test_normalize.py` | URL 规范化 | 去 utm/fragment、去空白、ID 稳定、默认名 |

运行方式：

```bash
pytest            # 全部
pytest tests/test_registry.py -q
```

---

## 14. 已知约束与扩展点

### 14.1 约束

| 约束 | 原因 |
|---|---|
| 必须能直连目标站/GitHub raw | 国内常不通，`github.raw_mirror` 与 `publish.mirror_base` 是两条缓解路径 |
| 多仓递归深度 ≤ `discover.max_depth`（默认 2） | 防止嵌套爆炸 |
| L3 每日只抽 `daily_quota` 个源 | 每源要打 3 个真实接口，全量成本不可接受 |
| 直播测活单轮 ≤ `max_per_run` | 首轮全覆盖需要分多天完成 |
| `check_log` 按保留期删除（默认 90 天） | 控制库体积；长期趋势只看 365 天内 |
| 快照路径必须相对 `snapshots/` | 库要跨机器/跨 CI 复用 |
| `.bat` / `.ps1` 必须纯 ASCII | Windows 代码页问题 |

### 14.2 扩展点（按改动成本从低到高）

1. **新增发布产物**：在 `publish.py` 加 `build_*` 纯函数，在 `cli.publish` 里写入 `output/`，配置项加到 `DEFAULTS["publish"]`。
2. **新增体检维度**：扩展 `SchemaInfo`（如统计 flags/EPG），在 `parse_config` 里填充，再在 `_score` 的 richness 里加权。
3. **新增通知渠道**：在 `cli.notify` 的渠道元组里加一行 + `publish.webhook_payload` 支持新 payload 形态。
4. **新增发现渠道**（如网页正则抓取）：仿 `github_hunt.Hunter` 写一个类，暴露 `run() -> stats`，在 cli 加命令；登记统一走 `Registry.upsert(origin=...)`。
5. **新增状态**：改 `registry.record` 的判定顺序 + `publish.STATUS_ORDER` / `_STATUS_CLASS` / `_STATUS_RANK`，注意看板卡片、报告表格、CSS class 三处都要同步。

### 14.3 改动时的注意事项

- 改 `settings` 结构时，**同时**改 `config.py:DEFAULTS` 与 `config/settings.yaml`，否则两者口径不一致（yaml 注释里也标了对应关系）。
- 改 `check_log.detail` 的键名会破坏历史统计（`recent_changes` 依赖 `"chg"`、`publish` 依赖 `"skip"`），需要兼容旧数据或做一次数据迁移。
- 改看板结构时，`tests/test_publish.py` 有大量断言直接匹配 HTML 片段（列名、`data-*` 属性、按钮 id），改完必须同步用例。
- 改数据库表必须走 `_migrate()` 的"缺列补列"路径，不要直接改 `_SCHEMA` 就假设旧库可用。

---

## 附录 A：一次典型本地运行

```bash
tvboxkeeper init
tvboxkeeper import
tvboxkeeper check --limit 20        # 先小样本确认网络
tvboxkeeper check
tvboxkeeper discover
tvboxkeeper livetest --max 200
tvboxkeeper publish
tvboxkeeper stats
tvboxkeeper history <id>
tvboxkeeper notify
```

## 附录 B：术语

| 术语 | 含义 |
|---|---|
| 单仓 / 接口 | 一个 TVBox 配置 JSON（含 `sites`/`parses`/`lives`/`spider`） |
| 多仓 / storeHouse | 顶层 `storeHouse[{sourceName, sourceUrl}]` 的"仓库的仓库" |
| spider jar | `type: 3` 站点依赖的可执行 jar，**只探测不执行** |
| 源（Source） | 本项目中一个被登记、跟踪的配置 URL |
| 镜像 | 本仓库保存的源内容快照副本，用于订阅抗失效 |
| 超级直播列表 | 多源频道归一合并、多线路互备的单个 txt |
