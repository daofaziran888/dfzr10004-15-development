"""采集合规配置接口：维护合规规则，覆盖提交审批、发布规则、废弃规则等动作。

发布规则走「发布前联合门禁」：先核对站点域名/访问阈值/并发上限/请求头模板，
再核对代理可用节点与冷却状态，最后列出受影响的采集计划。门禁未通过时只落一份
待调整清单，规则不会生效。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.compliance import get_service

router = APIRouter(prefix="/api/compliance", tags=["采集合规配置"])

service = get_service()

LIST_FIELDS = ["规则编号", "站点域名", "协议标识", "访问间隔", "并发上限", "请求头模板", "合规状态", "最近审查"]
STATUSES = ["草稿", "待审批", "已生效", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待审批、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤采集合规配置列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# 固定路径要注册在 /{entry_id} 之前，否则会被动态路径吃掉。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集合规配置清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "compliance", "total": total, "items": items}


@router.get("/{entry_id}/publish-gate", response_model=dict)
def preview_publish_gate(entry_id: int) -> dict:
    """发布前联合门禁预检：只计算不落库，用于发布前核对访问阈值、代理可用量与计划影响。"""
    gate, message = service.preview(entry_id)
    if gate is None:
        raise HTTPException(status_code=404, detail=message)
    return gate


@router.get("/{entry_id}/proxy-availability", response_model=dict)
def proxy_availability(entry_id: int) -> dict:
    """按规则站点列代理可用量：可用节点、排除原因与当前锁定规则。"""
    snapshot, message = service.proxy_availability(entry_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=message)
    return snapshot


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条合规规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"合规规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条合规规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="合规规则已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """调整草稿/待审批规则的字段，便于按门禁给出的待调整清单逐项修正后重新发布。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条合规规则执行提交审批、发布规则、废弃规则；不允许的动作会被拦下并说明原因。

    发布规则时门禁不通过返回 422 并附待调整清单；同一规则同一版本已有发布结果时
    返回 409，不重复落库。
    """
    action = str(payload.values.get("action") or "").strip()
    request_id = payload.values.get("request_id")
    request_id = str(request_id).strip() if request_id not in (None, "") else None
    entry, message, verdict = service.run_action(entry_id, action, request_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    if action == "发布规则" and verdict == "conflict":
        raise HTTPException(
            status_code=409,
            detail={"message": message, "entry": entry, "发布记录": entry.get("最近发布记录")},
        )
    if action == "发布规则" and verdict == "rejected":
        raise HTTPException(
            status_code=422,
            detail={
                "message": message,
                "entry": entry,
                "待调整清单": entry.get("待调整清单") or [],
            },
        )
    return ActionResult(ok=True, message=message, entry=entry)
