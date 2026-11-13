"""合规发布门禁与代理额度共用的计算口径。

这个模块只做「读数据、算额度、落锁定记录」三件事，不参与规则状态流转，
避免合规服务和代理服务互相 import。所有写操作由调用方持有同步锁后执行。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.config import settings
from app.store import store

COMPLIANCE_MODULE = "compliance"
PROXY_MODULE = "proxy"
TASKPLAN_MODULE = "taskplan"
PUBLISH_MODULE = "compliance_publish"
QUOTA_MODULE = "proxy_quota"

RULE_EFFECTIVE = "已生效"
RULE_RETIRED = "已废弃"
NODE_ENABLED = "已启用"
PLAN_FINISHED = "已完成"
PLAN_GRANTED = "已授权"
PLAN_REVOKED = "已失效"
PLAN_NOT_GRANTED = "未授权"
SCHEDULABLE = "可排班"
SCHEDULE_BLOCKED = "已阻断"

_DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(毫秒|ms|秒|s|分钟|min|m)?$")


def now_text() -> str:
    """统一的时间戳口径，精确到秒。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_domain(value: Any) -> str:
    """把站点域名归一成 host：去掉协议、路径、端口、查询串并转小写。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if ":" in text:
        text = text.split(":", 1)[0]
    return text.strip()


def domain_is_valid(domain: str) -> bool:
    return bool(domain) and len(domain) <= 255 and bool(_DOMAIN_RE.fullmatch(domain))


def parse_interval(value: Any) -> float | None:
    """解析访问间隔/采集频率，统一换算成秒；无法解析返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) >= 0 else None
    text = str(value or "").strip().lower()
    if not text:
        return None
    text = text.replace("每", "").replace(" ", "")
    match = _INTERVAL_RE.fullmatch(text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "s"
    if unit in {"毫秒", "ms"}:
        return number / 1000.0
    if unit in {"分钟", "min", "m"}:
        return number * 60.0
    return number


def format_interval(seconds: float | None) -> str:
    if seconds is None:
        return "无法解析"
    if seconds < 1 and seconds > 0:
        return f"{seconds * 1000:g}ms"
    return f"{seconds:g}s"


def parse_concurrency(value: Any) -> int | None:
    """并发上限只接受非负整数，小数和空值都视为不合法。"""
    if isinstance(value, bool):
        return None
    text = str(value or "").strip()
    if not re.fullmatch(r"\+?\d+", text):
        return None
    number = int(text)
    return number if number > 0 else None


def parse_ratio(value: Any) -> float | None:
    """可用率统一换算成百分数；「0.98」和「98%」都得到 98.0。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    percent_sign = "%" in text
    try:
        number = float(text.rstrip("%").strip())
    except ValueError:
        return None
    if percent_sign or number > 1:
        return number
    return number * 100.0


def parse_delay_ms(value: Any) -> float:
    """响应延迟统一换算成毫秒，用于同优先级节点的稳定排序。"""
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(毫秒|ms|秒|s)?", text)
    if not match:
        return float("inf")
    number = float(match.group(1))
    unit = match.group(2) or "ms"
    return number * 1000 if unit in {"秒", "s"} else number


def validate_header_template(value: Any) -> tuple[dict[str, str], list[str]]:
    """校验请求头模板：每行必须是 `名称: 值`，且必须包含 User-Agent。"""
    text = str(value or "").strip()
    errors: list[str] = []
    if not text:
        return {}, ["请求头模板为空"]
    headers: dict[str, str] = {}
    for raw_line in re.split(r"[\n;]", text):
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            errors.append(f"请求头行「{line}」缺少冒号分隔")
            continue
        name, header_value = line.split(":", 1)
        name = name.strip()
        header_value = header_value.strip()
        if not _HEADER_NAME_RE.fullmatch(name):
            errors.append(f"请求头名称「{name}」格式不合法")
            continue
        if not header_value:
            errors.append(f"请求头「{name}」的值为空")
            continue
        headers[name] = header_value
    required_header = settings.compliance_required_header.lower()
    if not any(name.lower() == required_header for name in headers):
        errors.append(f"请求头模板缺少必填的 {settings.compliance_required_header}")
    return headers, errors


def proxy_rows() -> list[dict[str, Any]]:
    return store.rows(PROXY_MODULE)


def quota_rows() -> list[dict[str, Any]]:
    return store.rows(QUOTA_MODULE)


def publish_rows() -> list[dict[str, Any]]:
    return store.rows(PUBLISH_MODULE)


def taskplan_rows() -> list[dict[str, Any]]:
    return store.rows(TASKPLAN_MODULE)


def locked_quota_for_node(node_id: int) -> int:
    return sum(
        int(row.get("锁定额度") or 0)
        for row in quota_rows()
        if int(row.get("节点ID") or 0) == node_id and row.get("状态") == "锁定"
    )


def node_capacity(row: dict[str, Any]) -> int:
    return parse_concurrency(row.get("并发额度")) or 0


def node_availability(row: dict[str, Any]) -> int:
    return max(node_capacity(row) - locked_quota_for_node(int(row.get("id") or 0)), 0)


def node_unavailable_reasons(row: dict[str, Any]) -> list[str]:
    """按判定顺序列出节点的全部不可用原因；空列表表示可参与代理分配。"""
    reasons: list[str] = []
    if row.get("status") != NODE_ENABLED:
        reasons.append(f"节点状态为「{row.get('status') or '未知'}」，只有已启用节点可用")
    cooldown = str(row.get("冷却状态") or "").strip()
    if cooldown and cooldown not in settings.proxy_cooldown_ready_states:
        reasons.append(f"冷却状态为「{cooldown}」，尚未结束冷却")
    availability = parse_ratio(row.get("可用率"))
    if availability is None:
        reasons.append("缺少可用率，无法判定可用性")
    elif availability < settings.compliance_min_proxy_availability:
        reasons.append(
            f"可用率 {availability:g}% 低于阈值 "
            f"{settings.compliance_min_proxy_availability:g}%"
        )
    if node_capacity(row) <= 0:
        reasons.append("未配置可用的并发额度")
    return reasons


def node_unavailable_reason(row: dict[str, Any]) -> str | None:
    """节点不可用原因合并成一句；None 表示节点可参与代理分配。"""
    reasons = node_unavailable_reasons(row)
    return "；".join(reasons) if reasons else None


def proxy_entry(row: dict[str, Any]) -> dict[str, Any]:
    """代理清单里的一行：带上容量、锁定、可用额度和锁定规则明细。"""
    node_id = int(row.get("id") or 0)
    locked = locked_quota_for_node(node_id)
    capacity = node_capacity(row)
    reason = node_unavailable_reason(row)
    locks = [
        {
            "规则编号": lock.get("规则编号"),
            "版本号": lock.get("版本号"),
            "锁定额度": int(lock.get("锁定额度") or 0),
        }
        for lock in quota_rows()
        if int(lock.get("节点ID") or 0) == node_id and lock.get("状态") == "锁定"
    ]
    enriched = dict(row)
    enriched.update(
        {
            "并发额度": capacity,
            "锁定额度": locked,
            "可用额度": max(capacity - locked, 0),
            "冷却状态": row.get("冷却状态") or "就绪",
            "绑定站点": row.get("绑定站点") or "",
            "可用标识": "不可用" if reason else "可用",
            "不可用原因": reason or "",
            "锁定规则": locks,
        }
    )
    return enriched


def proxy_snapshot(domain: str | None = None) -> dict[str, Any]:
    """按站点给出代理可用量：可用节点、被排除节点及排除原因。"""
    domain = normalize_domain(domain)
    usable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    total = 0
    for row in proxy_rows():
        node_id = int(row.get("id") or 0)
        capacity = node_capacity(row)
        locked = locked_quota_for_node(node_id)
        available = max(capacity - locked, 0)
        bound_site = normalize_domain(row.get("绑定站点"))
        base = {
            "节点编号": row.get("节点编号"),
            "节点ID": node_id,
            "绑定站点": bound_site,
            "并发额度": capacity,
            "锁定额度": locked,
            "可用额度": available,
            "可用率": parse_ratio(row.get("可用率")),
            "响应延迟": row.get("响应延迟"),
            "冷却状态": row.get("冷却状态") or "就绪",
        }
        reason = node_unavailable_reason(row)
        if reason:
            excluded.append({**base, "原因": reason})
            continue
        usable.append({**base, "站点匹配": "匹配" if (domain and bound_site == domain) else "通用"})
        total += available
    return {
        "站点域名": domain,
        "可用节点": usable,
        "不可用节点": excluded,
        "总可用额度": total,
        "可用率阈值": settings.compliance_min_proxy_availability,
        "判定时间": now_text(),
    }


def allocation_plan(required: int, domain: str) -> dict[str, Any]:
    """按「代理选择优先级」规划额度，不写任何锁定记录。

    优先级：绑定目标站点的节点 > 未绑定站点的通用节点 > 绑定其他站点的节点；
    同组内按可用率降序、响应延迟升序、节点编号升序，保证结果稳定可回归。
    """
    domain = normalize_domain(domain)
    candidates: list[tuple[int, float, float, int, dict[str, Any], int]] = []
    excluded: list[dict[str, Any]] = []
    for row in proxy_rows():
        reason = node_unavailable_reason(row)
        if reason:
            excluded.append({"节点编号": row.get("节点编号"), "原因": reason})
            continue
        node_id = int(row.get("id") or 0)
        available = node_availability(row)
        bound_site = normalize_domain(row.get("绑定站点"))
        if bound_site and bound_site == domain:
            group = 0
        elif not bound_site:
            group = 1
        else:
            group = 2
        availability = parse_ratio(row.get("可用率")) or 0.0
        candidates.append((group, -availability, parse_delay_ms(row.get("响应延迟")), node_id, row, available))
    candidates.sort(key=lambda item: item[:4])

    remaining = max(int(required), 0)
    items: list[dict[str, Any]] = []
    for group, neg_availability, delay, node_id, row, available in candidates:
        if remaining <= 0:
            break
        if available <= 0:
            continue
        allocated = min(remaining, available)
        items.append(
            {
                "节点ID": node_id,
                "节点编号": row.get("节点编号"),
                "绑定站点": normalize_domain(row.get("绑定站点")),
                "并发额度": node_capacity(row),
                "锁定额度（发布前）": locked_quota_for_node(node_id),
                "分配额度": allocated,
                "可用率": -neg_availability,
                "响应延迟": row.get("响应延迟"),
                "优先级组": group,
            }
        )
        remaining -= allocated

    total_available = sum(node_availability(row) for row in proxy_rows() if node_unavailable_reason(row) is None)
    return {
        "站点域名": domain,
        "需要额度": max(int(required), 0),
        "可分配额度": total_available,
        "缺口": max(max(int(required), 0) - total_available, 0),
        "已规划额度": sum(item["分配额度"] for item in items),
        "分配明细": items,
        "不可用节点": excluded,
        "优先级": [
            "第一优先：绑定目标站点的代理节点",
            "第二优先：未绑定站点的通用代理节点",
            "第三优先：绑定其他站点的代理节点（兜底）",
            "同组内按可用率降序、响应延迟升序、节点编号升序",
            "合规规则锁定额度优先于其他申请，锁定后不可被后续规则重复占用",
        ],
    }


def commit_allocation(rule: dict[str, Any], version: str, allocation: dict[str, Any]) -> list[dict[str, Any]]:
    """把规划好的额度落成锁定记录；调用方必须已持有全局同步锁。"""
    records: list[dict[str, Any]] = []
    rows = quota_rows()
    for item in allocation.get("分配明细", []):
        record = {
            "id": max((int(row.get("id") or 0) for row in rows), default=0) + 1,
            "规则ID": int(rule.get("id") or 0),
            "规则编号": rule.get("规则编号"),
            "版本号": version,
            "节点ID": item["节点ID"],
            "节点编号": item["节点编号"],
            "锁定额度": item["分配额度"],
            "状态": "锁定",
            "锁定时间": now_text(),
            "释放时间": None,
        }
        rows.append(record)
        records.append(record)
    _sync_proxy_locks()
    return records


def release_rule_quota(rule_id: int) -> list[dict[str, Any]]:
    """释放某条规则持有的全部额度；保留台账记录供审计。"""
    released: list[dict[str, Any]] = []
    for row in quota_rows():
        if int(row.get("规则ID") or 0) != rule_id or row.get("状态") != "锁定":
            continue
        row["状态"] = "已释放"
        row["释放时间"] = now_text()
        released.append(row)
    if released:
        _sync_proxy_locks()
    return released


def _sync_proxy_locks() -> None:
    """把台账中的锁定额度同步回代理节点行，列表与详情都读这份值。"""
    for row in proxy_rows():
        node_id = int(row.get("id") or 0)
        row["锁定额度"] = locked_quota_for_node(node_id)


def quotas_for_rule(rule_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in quota_rows()
        if int(row.get("规则ID") or 0) == rule_id and row.get("状态") == "锁定"
    ]


def plans_for_site(domain: str) -> list[dict[str, Any]]:
    """列出使用该站点的采集计划，附带采集间隔与排班资格。"""
    domain = normalize_domain(domain)
    plans: list[dict[str, Any]] = []
    for row in taskplan_rows():
        if normalize_domain(row.get("目标站点")) != domain:
            continue
        plans.append(plan_snapshot(row))
    plans.sort(key=lambda item: str(item.get("计划编号") or ""))
    return plans


def plan_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """采集计划的一行视图：固定带上启动资格与排班状态，列表/详情/影响清单共用。"""
    interval = parse_interval(row.get("采集频率"))
    result = dict(row)
    result.update(
        {
            "计划ID": int(row.get("id") or 0),
            "目标站点": row.get("目标站点"),
            "采集间隔秒": interval,
            "采集间隔": format_interval(interval),
            "计划状态": row.get("status"),
            "启动资格": row.get("启动资格") or "未授权",
            "排班状态": row.get("排班状态") or "待规则发布",
            "合规规则": row.get("合规规则") or "",
            "合规版本": row.get("合规版本") or "",
            "参与排班": row.get("status") != PLAN_FINISHED,
        }
    )
    return result


def bind_plans(rule: dict[str, Any], version: str) -> list[dict[str, Any]]:
    """规则生效后，站点下未完成且不冲突的计划获得启动资格。

    访问间隔低于规则访问间隔的计划不进入排班：门禁要求冲突先消除，正常发布流程下
    不存在这类计划；这里再挡一次，避免手工改数据绕过门禁。
    """
    domain = normalize_domain(rule.get("站点域名"))
    rule_interval = parse_interval(rule.get("访问间隔"))
    bound: list[dict[str, Any]] = []
    for row in taskplan_rows():
        if normalize_domain(row.get("目标站点")) != domain:
            continue
        if row.get("status") == PLAN_FINISHED:
            continue
        plan_interval = parse_interval(row.get("采集频率"))
        if rule_interval is not None and plan_interval is not None and plan_interval < rule_interval:
            row["启动资格"] = PLAN_NOT_GRANTED
            row["排班状态"] = SCHEDULE_BLOCKED
            continue
        row["合规规则"] = rule.get("规则编号")
        row["合规版本"] = version
        row["启动资格"] = PLAN_GRANTED
        row["排班状态"] = SCHEDULABLE
        bound.append(row)
    return bound


def revoke_plans(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """规则撤回后，曾受该规则约束的计划同步失去启动资格。

    运行中的计划一并转为已暂停，避免撤回后仍在进行中的采集继续跑。
    """
    code = str(rule.get("规则编号") or "")
    revoked: list[dict[str, Any]] = []
    for row in taskplan_rows():
        if str(row.get("合规规则") or "") != code:
            continue
        row["启动资格"] = PLAN_REVOKED
        row["排班状态"] = SCHEDULE_BLOCKED
        if row.get("status") == "运行中":
            row["status"] = "已暂停"
            row["计划状态"] = "已暂停"
            row["pending"] = True
        revoked.append(row)
    return revoked


def publish_record(rule_id: int, version: str | None = None) -> dict[str, Any] | None:
    matches = [
        row
        for row in publish_rows()
        if int(row.get("规则ID") or 0) == rule_id
        and row.get("结果") == "已发布"
        and (version is None or str(row.get("版本号")) == str(version))
    ]
    return matches[-1] if matches else None


def publish_records_for_rule(rule_id: int) -> list[dict[str, Any]]:
    return [row for row in publish_rows() if int(row.get("规则ID") or 0) == rule_id]


def append_publish_record(
    rule: dict[str, Any],
    version: str,
    allocation: dict[str, Any],
    impact: list[dict[str, Any]],
    *,
    source: str = "样例数据初始化",
) -> dict[str, Any]:
    rows = publish_rows()
    record = {
        "id": max((int(row.get("id") or 0) for row in rows), default=0) + 1,
        "规则ID": int(rule.get("id") or 0),
        "规则编号": rule.get("规则编号"),
        "版本号": version,
        "请求标识": None,
        "结果": RULE_EFFECTIVE,
        "来源": source,
        "发布时间": now_text(),
        "站点域名": normalize_domain(rule.get("站点域名")),
        "访问间隔": rule.get("访问间隔"),
        "并发上限": parse_concurrency(rule.get("并发上限")),
        "代理额度": [
            {"节点编号": item.get("节点编号"), "分配额度": item.get("分配额度")}
            for item in allocation.get("分配明细", [])
        ],
        "计划影响": [plan.get("计划编号") for plan in impact],
        "门禁": {"passed": True, "判定范围": [], "检查项": [], "待调整清单": [], "来源": source},
    }
    rows.append(record)
    return record


def ensure_bootstrap() -> None:
    """让样例数据里本来就「已生效」的规则拥有对应额度和计划绑定。

    真实项目由初始化脚本处理；这里在首次读写时补一次，保证规则详情、计划影响与
    代理可用量从第一屏起就对得上。
    """
    with store.lock:
        if store.bootstrapped:
            return
        # 先占标记，避免递归调用期间重复执行。
        store.bootstrapped = True
        _bootstrap()

def _bootstrap() -> None:
    for rule in list(store.rows(COMPLIANCE_MODULE)):
        if rule.get("status") != RULE_EFFECTIVE:
            continue
        version = str(rule.get("规则版本") or "v1")
        if publish_record(int(rule.get("id") or 0), version):
            continue
        required = parse_concurrency(rule.get("并发上限")) or 0
        allocation = allocation_plan(required, normalize_domain(rule.get("站点域名")))
        commit_allocation(rule, version, allocation)
        impact = plans_for_site(normalize_domain(rule.get("站点域名")))
        bind_plans(rule, version)
        append_publish_record(rule, version, allocation, impact)
