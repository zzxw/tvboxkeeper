# TVBoxKeeper

TVBox 源维护工具：从网上发现 TVBox 接口源，自动"体检"（可达性、格式、子资源健康），
维护一份带健康分的源库，并发布成可直接导入 TVBox 的多仓订阅文件和体检榜单。

产品文档见 [docs/产品文档.md](docs/产品文档.md)。

## 安装（Windows / Git Bash）

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -e ".[dev]"
```

## 快速上手（在仓库根目录运行）

```bash
tvboxkeeper init        # 初始化 data/ 与数据库
tvboxkeeper import      # 把 config/sources.yaml 的种子源登记进库
tvboxkeeper add https://example.com/config.json --name 示例源   # 手工添加
tvboxkeeper check       # 全量体检（默认 L2：可达 + 解析 + 子资源探测）
tvboxkeeper discover    # 展开库中多仓（storeHouse/urls）的子仓
tvboxkeeper hunt        # GitHub 挖源：搜仓库/代码，探测常见配置文件名
tvboxkeeper publish     # 生成 output/多仓.json 与 output/报告.md
tvboxkeeper history <url或id>   # 单源体检历史 + 30 天可用率
tvboxkeeper stats       # 整体统计：状态分布、失败原因、低可用源
tvboxkeeper list        # 查看源库
```

> `hunt` 可选设置 `GITHUB_TOKEN` 环境变量启用代码搜索并提高限流配额；
> 国内直连 `raw.githubusercontent.com` 不通时，在 settings.yaml 配置 `github.raw_mirror`。

## 状态说明

| 状态 | 含义 |
|---|---|
| new | 刚登记，还没体检 |
| active | 最近一次体检通过 |
| flaky | 失败中，但连续失败次数未达判死线 |
| dead | 连续 N 次失败（默认 3，可在 settings.yaml 调整） |
| invalid | 可达但解析不出 TVBox 配置结构 |
| banned | 人工封禁，不参与体检与发布 |

## 推送到 GitHub（启用每日自动体检）

1. 在 GitHub 新建**空仓库**（不要勾选 README 初始化）：
   - 仓库名建议 `tvboxkeeper`（与工具同名）或 `tvbox-sources`；
   - 要让 TVBox 直接订阅 `output/多仓.json`，仓库需为 **Public**（私有库的 raw 链接带 token，TVBox 拿不到）；
   - 只想先自用可建 Private，之后再改 Public。
2. 推送：

```bash
git remote add origin https://github.com/<你的用户名>/tvboxkeeper.git
git branch -M main
git push -u origin main
```

`.github/workflows/daily-check.yml` 会在每天北京时间 05:30 自动跑
`import → discover → hunt → check → publish` 并把产物提交回仓库，
之后可用 `https://raw.githubusercontent.com/<用户名>/tvboxkeeper/main/output/多仓.json`
作为 TVBox 订阅地址（国内可加 gh-proxy 前缀）。

## 安全与合规边界

- 只存元数据与可达性结论，不存储/分发任何音视频内容；
- **绝不执行**源配置引用的 spider jar（那是可执行代码），只做 HTTP 探测；
- 抓取到的配置一律按不可信数据处理（仅 JSON 解析，隔离存放在 `data/`）；
- 对目标站点限速（并发/超时/重试均可调）。
