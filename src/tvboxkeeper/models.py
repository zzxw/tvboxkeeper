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

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


@dataclass
class SchemaInfo:
    """配置文件解析结果。"""

    schema_type: str = "invalid"  # tvbox | storehouse | mixed | invalid
    sites_count: int = 0
    parses_count: int = 0
    lives_count: int = 0
    storehouse_count: int = 0
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
    content: bytes = b""  # 原始内容，供快照
