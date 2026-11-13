"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "智能采集质量管控平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 合规规则访问阈值：站点约定的最小请求间隔（秒），低于该值不允许发布。
    compliance_access_threshold_seconds: float = 1.0
    # 单条规则允许申请的并发上限范围（正整数，含边界）。
    compliance_min_concurrency: int = 1
    compliance_max_concurrency: int = 32
    # 代理节点参与分配的最低可用率（百分数）。
    compliance_min_proxy_availability: float = 90.0
    # 请求头模板必须包含的请求头（大小写不敏感）。
    compliance_required_header: str = "User-Agent"
    # 视为「冷却已结束」的冷却状态取值。
    proxy_cooldown_ready_states: tuple[str, ...] = ("就绪", "正常", "已就绪", "未冷却")


settings = Settings()
