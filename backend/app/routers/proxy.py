"""代理池管理接口：维护代理节点，覆盖提交校验、启用节点、停用节点等动作。

代理清单会同步展示合规规则锁定的额度：并发额度、锁定额度、可用额度与冷却状态，
锁定额度直接读发布台账，避免两个入口各存一份数字。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import quota
from app.services.proxy import ProxyService

router = APIRouter(prefix="/api/proxy", tags=["代理池管理"])

service = ProxyService()

LIST_FIELDS = ["节点编号", "代理地址", "代理类型", "地理位置", "响应延迟", "可用率", "并发额度", "锁定额度", "可用额度", "冷却状态", "节点状态"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按节点编号与状态过滤代理池管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 固定路径要注册在 /{entry_id} 之前，否则会被动态路径吃掉。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出代理池管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "proxy", "total": total, "items": items}


@router.get("/quota-summary", response_model=dict)
def quota_summary() -> dict[str, Any]:
    """代理额度概览：各节点容量、被合规规则锁定的额度与剩余可用额度。

    这是与合规规则同步的另一个入口，锁定额度直接读发布台账，不另存一份。
    """
    quota.ensure_bootstrap()
    snapshot = quota.proxy_snapshot()
    nodes = [quota.proxy_entry(row) for row in quota.proxy_rows()]
    return {
        "判定时间": snapshot["判定时间"],
        "可用率阈值": snapshot["可用率阈值"],
        "节点": nodes,
        "合计": {
            "并发额度": sum(int(node["并发额度"]) for node in nodes),
            "锁定额度": sum(int(node["锁定额度"]) for node in nodes),
            "可用额度": sum(int(node["可用额度"]) for node in nodes),
            "可用节点": sum(1 for node in nodes if node["可用标识"] == "可用"),
            "不可用节点": sum(1 for node in nodes if node["可用标识"] == "不可用"),
        },
        "锁定台账": quota.quota_rows(),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条代理节点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"代理节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条代理节点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="代理节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条代理节点执行提交校验、启用节点、停用节点；不允许的动作会被拦下并说明原因。

    仍被合规规则锁定额度的节点不允许停用，避免已生效规则背后的额度凭空消失。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
