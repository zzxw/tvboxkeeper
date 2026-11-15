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
tvboxkeeper check --level L3   # 深度抽测：真实请求站点 CMS 接口
tvboxkeeper discover    # 展开库中多仓（storeHouse/urls）的子仓
tvboxkeeper hunt        # GitHub 挖源：搜仓库/代码，探测常见配置文件名
tvboxkeeper publish     # 生成 多仓.json / 直播.json / 报告.md / site看板 / mirrors 镜像
tvboxkeeper notify      # 推送最近发布的变化（Actions Summary / Telegram 可选）
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
| anti_crawl | 返回 HTML 反爬页（可能仍存活），判死时限放宽 3 倍 |
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
`import → discover → hunt → check → publish → notify` 并把产物提交回仓库。

- **静态看板**：Actions 自动部署到 GitHub Pages（首次需在仓库
  Settings → Pages → Source 选 **GitHub Actions**），地址形如
  `https://zzxw.github.io/tvboxkeeper/`；
- **变化通知**：默认写入 Actions 运行摘要；配 Secrets 后可推送到
  Telegram（`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`）或国内直连的
  飞书 / 钉钉 / 企业微信机器人（`FEISHU_WEBHOOK_URL` /
  `DINGTALK_WEBHOOK_URL` / `WECOM_WEBHOOK_URL`，填机器人 webhook 完整地址）；
- **本地兜底**：`powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1`
  注册 Windows 计划任务（每天 06:10，错过开机补跑），与 Actions 互为备份。
  跑批完成后**自动把产物回推远端**（pull --rebase，冲突或离线时安全放弃、
  绝不覆盖远端），日志在 `data/local-run.log`，卸载见脚本注释。
  注意 `scripts\daily-task.bat` 必须保持**纯 ASCII**——cmd 按 OEM 代码页解析
  .bat，写中文会破坏路径；
- **钉钉加签**：机器人安全设置选"加签"时，另配 Secret
  `DINGTALK_SECRET` 即可自动签名；
- **失败告警**：Actions 跑批失败时自动向已配置的通知渠道发告警
  （`notify --event failed`），订阅过期不再静默发生；
- **通知静默**：无变化且无新鲜周报时不推送通知渠道（`notify.silent_when_unchanged`，
  GITHUB_STEP_SUMMARY 照常写入），避免"狼来了"；
- **周报**：每周日 `publish` 自动生成 `output/周报.md`
  （7 天可用率趋势 / 内容变化 / 失败原因与主机），notify 当日一并推送；
- **单源详情页**：看板表格点击源名进入 `site/source-<id>.html`，
  含体检历史与内容变更记录；
- **同内容归并**：内容完全相同的源只保留最优一个进订阅（报告里列出归并关系）；
- **数据生命周期**：体检日志/快照/挖源记录默认保留 90 天（settings.yaml
  `cleanup` 可调），publish 时自动清理并 VACUUM 瘦身。

## 镜像发布（订阅抗失效）

配置 `publish.mirror_base`（指向本仓库 `output/mirrors/` 的 raw 地址）后，
`publish` 会把每个订阅源最后一次成功的内容快照写入 `output/mirrors/<id>.json`，
多仓条目改用镜像地址——**原站失效时订阅依然可用**（内容为最后一次成功快照）。
镜像与源 ID 的对应关系见 `output/mirrors/index.json`。

## 直播源维护

直播文件（`#genre#` txt / m3u）是一等源：`discover` 会从活跃配置的
`lives[].url` 自动登记直播源，体检时统计频道/分组数并抽样测流（L2），
同样参与健康分、镜像、归并。`publish` 额外生成 `output/直播.json`
（只含直播的合集配置）与 `output/super_live.txt` **超级直播列表**
（多源频道名归一合并：CCTV-1/央视一套/CCTV1综合 → 同一频道，
多线路 `#` 互备，央视/卫视/其他分组排序）。

## 深度验证与合规

- **jar 完整性校验**：配置里 `spider: url;md5;hash` 带哈希时，全量下载
  jar 校验 md5 一致性——jar 被静默换包能发现（200 状态不算过）；
- **parses 测活**：解析接口纳入 L2 探测，播放链路更完整；
- **合规黑名单**：体检扫描配置内容里的敏感关键词（`settings.yaml
  compliance`，保守匹配），命中自动 banned，降低公开仓库风险。

## 安全与合规边界

- 只存元数据与可达性结论，不存储/分发任何音视频内容；
- **绝不执行**源配置引用的 spider jar（那是可执行代码），只做 HTTP 探测；
- 抓取到的配置一律按不可信数据处理（仅 JSON 解析，隔离存放在 `data/`）；
- 对目标站点限速（并发/超时/重试均可调）。
