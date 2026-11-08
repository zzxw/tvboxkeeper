"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProbeResult:
    """单个子资源（jar/直播/wallpaper）的探测结果。"""

    url: str
    kind: str  # jar | live | wallpaper
    status: int = 0
    latency_ms: int = 0
    error: str = ""
    content_ok: bool = True  # 内容特征校验（jar 魔数/直播列表特征）

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400 and self.content_ok


@dataclass
class SchemaInfo:
    """配置文件解析结果。"""

    schema_type: str = "invalid"  # tvbox | storehouse | mixed | live | anti_crawl | empty | invalid
    sites_count: int = 0
    parses_count: int = 0
    lives_count: int = 0
    storehouse_count: int = 0
    # 直播文件（schema_type=live）：频道数 / 分组数
    live_channels: int = 0
    live_groups: int = 0
    # L3 抽测候选：type 0/1 站点的 http(s) CMS api（最多收集 10 个）
    site_apis: list[str] = field(default_factory=list)
    # 分支适配标注：通用 / 偏影视仓 / 需特定分支（多仓为空）
    compat: str = ""
    # L2 待探测的子资源 (kind, url)，顺序即优先级
    probes: list[tuple[str, str]] = field(default_factory=list)
    # 多仓的子仓条目 [{"sourceName": ..., "sourceUrl": ...}]
    children: list[dict] = field(default_factory=list)


@dataclass
class CheckOutcome:
    """一次体检的完整结果。"""

    url: str
    ok: bool = False
    status: int = 0  # HTTP 状态码，0 表示网络层失败
    latency_ms: int = 0
    error: str = ""
    schema: SchemaInfo | None = None
    probes: list[ProbeResult] = field(default_factory=list)
    l3_tested: int = 0  # L3 抽测的站点接口数
    l3_ok: int = 0      # L3 抽测通过的站点接口数
    content: bytes = b""  # 原始内容，供快照
