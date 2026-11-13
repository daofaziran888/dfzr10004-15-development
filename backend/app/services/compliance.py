"""采集合规配置业务规则：状态流转、字段校验与发布前联合门禁都收在这里。

发布门禁把「合规规则 → 站点约定 → 代理节点 → 采集计划」这条数据链一次走完：
规则自身的站点域名/访问间隔/并发上限/请求头模板先过准入，再按站点核对代理节点的
可用节点、容量与冷却状态并规划锁定额度，最后列出使用该站点的采集计划及访问间隔冲突。
任一条件不满足时规则不会生效，只落一份待调整清单。
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.config import settings
from app.services import quota
from app.store import store

MODULE = "compliance"
REQUIRED_FIELDS = ["规则编号", "站点域名", "协议标识"]
STATUS_ORDER = ["草稿", "待审批", "已生效", "已废弃"]
ACTION_RULES = {"提交审批": "待审批", "发布规则": "已生效", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = ["废弃规则"]
PROTOCOLS = {"https": "HTTPS", "http": "HTTP"}
PUBLISH_VERDICT = "已发布"
ADJUST_VERDICT = "待调整"
EDITABLE_STATUSES = {"草稿", "待审批"}
EDITABLE_FIELDS = ["站点域名", "协议标识", "访问间隔", "并发上限", "请求头模板", "最近审查"]


class ComplianceService:
    # ------------------------------------------------------------------ 查询
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.serialize(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        quota.ensure_bootstrap()
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        detail = self.serialize(row)
        detail["发布记录"] = [dict(record) for record in quota.publish_records_for_rule(entry_id)]
        detail["当前锁定"] = quota.quotas_for_rule(entry_id)
        detail["代理可用量"] = quota.proxy_snapshot(row.get("站点域名"))
        detail["计划影响"] = quota.plans_for_site(row.get("站点域名"))
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("访问间隔", "并发上限", "请求头模板", "合规状态", "最近审查"):
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self.serialize(entry), []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """调整规则字段，为门禁失败后的「待调整清单」提供修改入口。

        只允许在草稿/待审批状态下调整；已生效规则必须先废弃并重新登记，避免
        已锁定的代理额度与已授权的计划被悄悄改写。
        """
        quota.ensure_bootstrap()
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"合规规则 {entry_id} 不存在或已归档"
            if entry.get("status") not in EDITABLE_STATUSES:
                return (
                    None,
                    f"规则 {entry.get('规则编号')} 当前状态为「{entry.get('status')}」，"
                    "只有草稿或待审批状态允许调整",
                )
            changed = False
            for field in EDITABLE_FIELDS:
                if field in values:
                    entry[field] = values.get(field)
                    changed = True
            if not changed:
                return None, "没有可调整的字段"
            entry.pop("门禁", None)
            entry["待调整清单"] = []
            entry["最近发布结果"] = None
            return self.serialize(entry), f"合规规则 {entry.get('规则编号')} 已调整，可重新发布"

    # ------------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, str]:
        """执行动作并返回 (规则, 提示语, 结果类型)，结果类型取 ok/conflict/rejected。"""
        quota.ensure_bootstrap()
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"合规规则 {entry_id} 不存在或已归档", "rejected"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于采集合规配置可执行范围", "rejected"
            if action == "发布规则":
                return self.publish(entry, request_id)
            if action == "废弃规则":
                return self.retire(entry)
            target = ACTION_RULES[action]
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return self.serialize(entry), f"合规规则已{action}", "ok"

    def publish(
        self,
        entry: dict[str, Any],
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, str]:
        """发布规则：门禁通过才生效，同一规则同一版本只落一份发布结果。

        并发口径由全局锁 + 版本号共同保证：
        - 同一规则同一版本并发发布时，先拿到锁的请求落库，后到的请求看到已有结果后
          返回 409 冲突，不会出现两份发布记录；
        - 两个规则并发申请同一批代理额度时，先拿到锁的请求先锁定额度，后到的请求在
          门禁的「代理可用额度」判定里被拦下，只生成待调整清单。
        """
        versions = self.versions_of(entry)
        existing = next(
            (
                record
                for record in reversed(quota.publish_records_for_rule(int(entry.get("id") or 0)))
                if record.get("结果") == PUBLISH_VERDICT and str(record.get("版本号")) in versions
            ),
            None,
        )

        if existing is not None:
            return (
                self.serialize(entry),
                (
                    f"规则 {entry.get('规则编号')} {existing.get('版本号')} 已有发布结果"
                    f"（记录 {existing.get('id')}，请求 {existing.get('请求标识') or '未知'}），"
                    "同一版本只落一份结果，本次请求返回冲突"
                ),
                "conflict",
            )
        if entry.get("status") == quota.RULE_EFFECTIVE:
            return (
                self.serialize(entry),
                f"规则 {entry.get('规则编号')} 已处于生效状态，本次请求不重复发布",
                "conflict",
            )

        gate = self.evaluate_gate(entry)
        version = self.version_of(entry)
        record = self._record(entry, version, request_id, gate)
        entry["最近发布结果"] = record["结果"]
        entry["最近发布版本"] = version
        entry["最近发布记录"] = record["id"]
        entry["门禁"] = gate

        if not gate["passed"]:
            entry["status"] = "待审批"
            entry["pending"] = True
            entry["abnormal"] = True
            entry["待调整清单"] = gate["待调整清单"]
            reasons = "；".join(gate["失败原因"]) or "发布门禁未通过"
            return (
                self.serialize(entry),
                f"规则 {entry.get('规则编号')} 未通过发布前联合门禁：{reasons}，已生成待调整清单",
                "rejected",
            )

        allocation = gate["代理额度规划"]
        committed = quota.commit_allocation(entry, version, allocation)
        entry["status"] = quota.RULE_EFFECTIVE
        entry["pending"] = False
        entry["abnormal"] = False
        entry["合规状态"] = "已生效"
        entry["规则版本"] = version
        entry["待调整清单"] = []
        bound = quota.bind_plans(entry, version)
        # 发布后按实际锁定台账回填，保证规则详情、计划影响与代理可用量三处口径一致。
        locked_total = sum(int(row.get("锁定额度") or 0) for row in committed)
        gate["已锁定额度"] = locked_total
        gate["代理额度规划"]["已规划额度"] = locked_total
        gate["代理可用量"] = quota.proxy_snapshot(entry.get("站点域名"))
        gate["计划影响"] = quota.plans_for_site(entry.get("站点域名"))
        record["代理额度"] = [
            {"节点编号": row.get("节点编号"), "分配额度": row.get("锁定额度")} for row in committed
        ]
        record["计划影响"] = [plan.get("计划编号") for plan in gate["计划影响"]]
        record["锁定额度"] = locked_total
        record["门禁"] = gate
        entry["门禁"] = gate
        return (
            self.serialize(entry),
            (
                f"规则 {entry.get('规则编号')} {version} 已发布，锁定 {locked_total} "
                f"个代理额度，{len(bound)} 个采集计划获得启动资格"
            ),
            "ok",
        )

    def retire(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str, str]:
        """废弃规则：释放额度，并让受该规则约束的计划同步失去启动资格。"""
        revoked = quota.revoke_plans(entry)
        released = quota.release_rule_quota(int(entry.get("id") or 0))
        entry["status"] = quota.RULE_RETIRED
        entry["pending"] = False
        entry["abnormal"] = True
        entry["合规状态"] = "已废弃"
        entry["撤回时间"] = quota.now_text()
        return (
            self.serialize(entry),
            (
                f"规则 {entry.get('规则编号')} 已废弃，释放 "
                f"{sum(int(row.get('锁定额度') or 0) for row in released)} 个代理额度，"
                f"{len(revoked)} 个采集计划失去启动资格"
            ),
            "ok",
        )

    def _record(
        self,
        entry: dict[str, Any],
        version: str,
        request_id: str | None,
        gate: dict[str, Any],
    ) -> dict[str, Any]:
        """写入一条发布/待调整记录；同一版本未通过门禁的记录原地替换，避免堆积。"""
        rows = quota.publish_rows()
        previous = next(
            (
                row
                for row in reversed(rows)
                if int(row.get("规则ID") or 0) == int(entry.get("id") or 0)
                and str(row.get("版本号")) == version
                and row.get("结果") == ADJUST_VERDICT
            ),
            None,
        )
        record = {
            "id": previous["id"] if previous else max((int(row.get("id") or 0) for row in rows), default=0) + 1,
            "规则ID": int(entry.get("id") or 0),
            "规则编号": entry.get("规则编号"),
            "版本号": version,
            "请求标识": request_id or None,
            "站点域名": quota.normalize_domain(entry.get("站点域名")),
            "访问间隔": entry.get("访问间隔"),
            "并发上限": gate.get("并发上限"),
            "结果": PUBLISH_VERDICT if gate["passed"] else ADJUST_VERDICT,
            "来源": "发布规则",
            "发布时间": quota.now_text(),
            "门禁": gate,
            "代理额度": [],
            "计划影响": [plan.get("计划编号") for plan in gate["计划影响"]],
        }
        if previous is not None:
            previous.clear()
            previous.update(record)
            return previous
        rows.append(record)
        return record

    # ------------------------------------------------------------------ 门禁
    def access_threshold(self, entry: dict[str, Any]) -> float:
        """访问阈值：站点默认阈值、同站点已生效规则与本规则自身的最大访问间隔取高者。

        把规则自身的访问间隔也算进去，是为了让「访问阈值」始终等于本次实际约束：
        要求 5s 的规则，其阈值就是 5s，而不是被站点默认值兜底成 1s。
        """
        baseline = settings.compliance_access_threshold_seconds
        domain = quota.normalize_domain(entry.get("站点域名"))
        own = quota.parse_interval(entry.get("访问间隔"))
        peers = [
            quota.parse_interval(row.get("访问间隔"))
            for row in store.rows(MODULE)
            if row is not entry
            and row.get("status") == quota.RULE_EFFECTIVE
            and quota.normalize_domain(row.get("站点域名")) == domain
        ]
        candidates = [value for value in [*peers, own] if value is not None]
        return max([baseline, *candidates])

    def evaluate_gate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """发布前联合门禁：逐项判定并返回判定范围、允许保存标准与待调整清单。"""
        domain = quota.normalize_domain(entry.get("站点域名"))
        protocol = str(entry.get("协议标识") or "").strip().lower()
        interval = quota.parse_interval(entry.get("访问间隔"))
        threshold = self.access_threshold(entry)
        concurrency = quota.parse_concurrency(entry.get("并发上限"))
        headers, header_errors = quota.validate_header_template(entry.get("请求头模板"))
        checks: list[dict[str, Any]] = []
        adjustments: list[dict[str, Any]] = []
        counts = {"站点": 0, "代理": 0, "计划": 0}

        def fail(scope: str, condition: str, expected: str, actual: str, hint: str, **extra: Any) -> None:
            counts[scope] = counts.get(scope, 0) + 1
            checks.append(
                {
                    "判定范围": scope,
                    "条件": condition,
                    "要求": expected,
                    "实测": actual,
                    "判定": "不满足",
                    "说明": hint,
                    **extra,
                }
            )
            adjustments.append(
                {"范围": scope, "条件": condition, "要求": expected, "实测": actual, "调整建议": hint}
            )

        def ok(scope: str, condition: str, expected: str, actual: str, **extra: Any) -> None:
            checks.append(
                {
                    "判定范围": scope,
                    "条件": condition,
                    "要求": expected,
                    "实测": actual,
                    "判定": "满足",
                    **extra,
                }
            )

        # 1. 站点域名与协议标识：所有后续判定都以域名为主键。
        if not quota.domain_is_valid(domain):
            fail("站点", "站点域名", "可解析的主机名", str(entry.get("站点域名") or "空"), "补全站点域名，例如 example.com")
        else:
            ok("站点", "站点域名", "可作为判定主键", domain)
        if protocol not in PROTOCOLS:
            fail("站点", "协议标识", "http/https", protocol or "空", "协议标识只支持 HTTP 或 HTTPS")
        else:
            ok("站点", "协议标识", "http/https", PROTOCOLS[protocol])

        # 2. 访问阈值与访问间隔：低于阈值即为冲突，不允许发布。
        if interval is None:
            fail(
                "站点",
                "访问间隔",
                f"≥ {quota.format_interval(threshold)}",
                str(entry.get("访问间隔") or "空"),
                "访问间隔需为不小于 0 的数字，可带 秒/s、毫秒/ms、分钟/min 单位",
            )
        elif interval < threshold:
            fail(
                "站点",
                "访问间隔",
                f"≥ {quota.format_interval(threshold)}",
                quota.format_interval(interval),
                f"站点 {domain} 的访问阈值是 {quota.format_interval(threshold)}，调大访问间隔后再发布",
            )
        else:
            ok(
                "站点",
                "访问间隔",
                f"≥ {quota.format_interval(threshold)}",
                quota.format_interval(interval),
                **{"阈值来源": "站点默认阈值与同站点已生效规则的最大访问间隔取高者"},
            )

        # 3. 并发上限的允许保存范围。
        if concurrency is None:
            fail(
                "站点",
                "并发上限",
                f"{settings.compliance_min_concurrency}~{settings.compliance_max_concurrency} 的整数",
                str(entry.get("并发上限") or "空"),
                "并发上限只接受正整数",
            )
        elif not settings.compliance_min_concurrency <= concurrency <= settings.compliance_max_concurrency:
            fail(
                "站点",
                "并发上限",
                f"{settings.compliance_min_concurrency}~{settings.compliance_max_concurrency}",
                str(concurrency),
                f"调整并发上限到 {settings.compliance_min_concurrency}~{settings.compliance_max_concurrency} 之间",
            )
        else:
            ok(
                "站点",
                "并发上限",
                f"{settings.compliance_min_concurrency}~{settings.compliance_max_concurrency}",
                str(concurrency),
            )

        # 4. 请求头模板：必须能解析成键值对，且包含约定请求头。
        if header_errors:
            fail(
                "站点",
                "请求头模板",
                f"每行 `名称: 值`，且包含 {settings.compliance_required_header}",
                "；".join(header_errors),
                "按 `名称: 值` 逐行填写请求头，并补齐 User-Agent",
            )
        else:
            ok("站点", "请求头模板", f"包含 {settings.compliance_required_header}", f"{len(headers)} 个请求头")

        # 5. 代理额度：按站点核对可用节点、容量与冷却状态。
        allocation = quota.allocation_plan(concurrency or 0, domain)
        if concurrency is not None:
            if allocation["可分配额度"] < concurrency:
                fail(
                    "代理",
                    "代理可用额度",
                    f"≥ 需要额度 {concurrency}",
                    f"可用 {allocation['可分配额度']}，缺口 {allocation['缺口']}",
                    "先启用或结束冷却更多代理节点，或下调并发上限",
                    **{"可用节点": allocation["分配明细"], "不可用节点": allocation["不可用节点"]},
                )
            elif not allocation["分配明细"]:
                fail(
                    "代理",
                    "代理节点可用性",
                    "至少 1 个已启用且不在冷却的节点",
                    "没有可分配额度的代理节点",
                    "启用代理节点或结束冷却后再发布",
                    **{"不可用节点": allocation["不可用节点"]},
                )
            else:
                ok(
                    "代理",
                    "代理可用额度",
                    f"≥ 需要额度 {concurrency}",
                    f"可用 {allocation['可分配额度']}，已规划 {allocation['已规划额度']}",
                    **{"分配明细": allocation["分配明细"], "不可用节点": allocation["不可用节点"]},
                )

        # 6. 计划影响清单与访问间隔冲突。
        plans = quota.plans_for_site(domain)
        conflicts: list[dict[str, Any]] = []
        if interval is not None:
            for plan in plans:
                if plan["采集间隔秒"] is not None and plan["采集间隔秒"] < interval:
                    conflicts.append(
                        {
                            "计划编号": plan["计划编号"],
                            "目标站点": plan["目标站点"],
                            "采集频率": plan["采集频率"],
                            "采集间隔": plan["采集间隔"],
                            "规则访问间隔": quota.format_interval(interval),
                            "调整建议": f"把采集频率调到 ≥ {quota.format_interval(interval)}",
                        }
                    )
        if conflicts:
            counts["计划"] = len(conflicts)
            checks.append(
                {
                    "判定范围": "计划",
                    "条件": "访问间隔冲突",
                    "要求": f"采集间隔 ≥ {quota.format_interval(interval)}",
                    "实测": f"{len(conflicts)} 个计划低于规则间隔",
                    "判定": "不满足",
                    "说明": "冲突计划需先调整采集频率，规则在冲突消除前不能发布",
                    "计划": conflicts,
                }
            )
            adjustments.extend(
                {"范围": "计划", "条件": "访问间隔冲突", **item} for item in conflicts
            )
        else:
            ok("计划", "访问间隔冲突", "无计划低于规则访问间隔", f"{len(plans)} 个计划待授权")

        return {
            "passed": not adjustments,
            "判定时间": quota.now_text(),
            "判定范围": [
                "站点域名与协议标识",
                "访问间隔与站点访问阈值",
                "并发上限允许范围",
                "请求头模板",
                "代理可用节点、容量与冷却状态",
                "站点下的采集计划访问间隔",
            ],
            "访问阈值秒": threshold,
            "访问阈值": quota.format_interval(threshold),
            "阈值来源": "站点默认阈值与同站点已生效规则的最大访问间隔取高者",
            "允许保存标准": {
                "访问间隔": f"≥ {quota.format_interval(threshold)}",
                "并发上限": f"{settings.compliance_min_concurrency}~{settings.compliance_max_concurrency} 的整数",
                "请求头模板": f"每行 `名称: 值` 且包含 {settings.compliance_required_header}",
                "代理可用额度": "可用节点额度之和 ≥ 并发上限",
                "代理节点": (
                    f"已启用、冷却状态为 {settings.proxy_cooldown_ready_states[0]}、"
                    f"可用率 ≥ {settings.compliance_min_proxy_availability:g}%"
                ),
                "计划影响": "站点下采集计划的采集间隔不得低于规则访问间隔",
            },
            "冲突口径": (
                "访问间隔冲突取两种判定：规则访问间隔低于站点访问阈值；"
                "站点下采集计划的采集间隔低于规则访问间隔。任一冲突都阻止发布，"
                "并逐条列入待调整清单"
            ),
            "并发上限": concurrency,
            "代理配额需求": concurrency,
            "代理额度规划": allocation,
            "代理可用量": quota.proxy_snapshot(domain),
            "计划影响": plans,
            "访问间隔冲突": conflicts,
            "检查项": checks,
            "待调整清单": adjustments,
            "失败原因": [item["说明"] for item in checks if item["判定"] == "不满足"],
            "计数": counts,
        }

    # ------------------------------------------------------------------ 序列化
    def version_of(self, entry: dict[str, Any]) -> str:
        signature = "|".join(
            str(entry.get(field) or "")
            for field in ("规则编号", "站点域名", "协议标识", "访问间隔", "并发上限", "请求头模板")
        )
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:6]
        return f"v-sha{digest}"

    def versions_of(self, entry: dict[str, Any]) -> set[str]:
        """版本口径：字段指纹 + 规则行上记录的发布版本，兼容样例数据里的 v1。"""
        versions = {self.version_of(entry)}
        if entry.get("规则版本"):
            versions.add(str(entry.get("规则版本")))
        return versions

    def serialize(self, entry: dict[str, Any]) -> dict[str, Any]:
        result = dict(entry)
        result["站点域名"] = quota.normalize_domain(entry.get("站点域名"))
        result["访问间隔"] = entry.get("访问间隔")
        result["并发上限"] = entry.get("并发上限")
        result["协议标识"] = str(entry.get("协议标识") or "").lower()
        result["合规状态"] = entry.get("合规状态") or entry.get("status")
        result["规则版本"] = entry.get("规则版本") or self.version_of(entry)
        result["启动门槛"] = "规则已生效且计划获得授权后方可启动"
        return result

    # ------------------------------------------------------------------ 预览
    def preview(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """发布前预检：只计算不落库，不锁额度也不改状态。"""
        quota.ensure_bootstrap()
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"合规规则 {entry_id} 不存在或已归档"
            gate = self.evaluate_gate(entry)
            gate["规则编号"] = entry.get("规则编号")
            gate["规则版本"] = self.version_of(entry)
            gate["当前状态"] = entry.get("status")
            gate["可发布"] = gate["passed"] and entry.get("status") != quota.RULE_EFFECTIVE
            return gate, "发布前门禁预检完成，未写入任何额度或状态"

    def proxy_availability(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """按规则站点给出代理可用量，供另一个入口同步核对锁定情况。"""
        quota.ensure_bootstrap()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"合规规则 {entry_id} 不存在或已归档"
        snapshot = quota.proxy_snapshot(entry.get("站点域名"))
        concurrency = quota.parse_concurrency(entry.get("并发上限")) or 0
        snapshot.update(
            {
                "规则编号": entry.get("规则编号"),
                "并发上限": concurrency,
                "锁定中额度": quota.quotas_for_rule(int(entry.get("id") or 0)),
                "计划影响": quota.plans_for_site(entry.get("站点域名")),
            }
        )
        return snapshot, "代理可用量按当前锁定额度实时计算"


_service: ComplianceService | None = None


def get_service() -> ComplianceService:
    global _service
    if _service is None:
        _service = ComplianceService()
    return _service
