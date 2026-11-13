"""采集任务编排业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services import quota
from app.store import store

MODULE = "taskplan"
REQUIRED_FIELDS = ["计划编号", "目标站点", "采集频率"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]
ACTION_RULES = {"启动计划": "运行中", "暂停计划": "已暂停", "归档计划": "已完成"}
ACTION_MESSAGES = {"启动计划": "已启动", "暂停计划": "已暂停", "归档计划": "已归档"}
NEGATIVE_ACTIONS = []
STARTABLE_STATUSES = {"草稿", "已暂停"}


class TaskplanService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        quota.ensure_bootstrap()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [quota.plan_snapshot(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        quota.ensure_bootstrap()
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        detail = quota.plan_snapshot(row)
        detail["启动阻断原因"] = self.start_block_reason(row) or ""
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.setdefault("启动资格", "未授权")
        entry.setdefault("排班状态", "待规则发布")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return quota.plan_snapshot(entry), []

    def start_block_reason(self, entry: dict[str, Any]) -> str | None:
        """启动计划的前置条件：所在站点的合规规则必须已生效并授权该计划。

        规则撤回后计划会被标记「已失效」，因此这条检查同时覆盖撤回场景。
        """
        if entry.get("启动资格") != quota.PLAN_GRANTED:
            rule = str(entry.get("合规规则") or "")
            if rule:
                return (
                    f"采集计划 {entry.get('计划编号')} 的合规规则 {rule} 未生效或已撤回，"
                    f"启动资格为「{entry.get('启动资格') or '未授权'}」，不能启动"
                )
            return (
                f"采集计划 {entry.get('计划编号')} 所在站点 {entry.get('目标站点')} "
                "没有已生效的采集合规规则，不能启动"
            )
        rule_code = str(entry.get("合规规则") or "")
        rule = next(
            (row for row in store.rows("compliance") if str(row.get("规则编号")) == rule_code),
            None,
        )
        if rule is None or rule.get("status") != quota.RULE_EFFECTIVE:
            return f"采集计划 {entry.get('计划编号')} 的合规规则 {rule_code} 已不是生效状态，不能启动"
        return None

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        quota.ensure_bootstrap()
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"采集计划 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于采集任务编排可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            if action == "启动计划":
                if entry.get("status") not in STARTABLE_STATUSES:
                    return (
                        None,
                        f"采集计划 {entry.get('计划编号')} 当前状态为「{entry.get('status')}」，"
                        "只有草稿或已暂停的计划可以启动",
                    )
                reason = self.start_block_reason(entry)
                if reason:
                    return None, reason
            entry["status"] = target
            entry["计划状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return quota.plan_snapshot(entry), f"采集计划{ACTION_MESSAGES[action]}"
