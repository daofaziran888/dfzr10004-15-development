"""发布前联合门禁的回归用例。

重点覆盖：
- COMP-0002 在 PROX-0002 不可用时的门禁判定与待调整清单；
- 同一版本并发发布只落一份结果，另一请求返回 409；
- 规则生效后计划才能启动，规则废弃后计划同步失去启动资格；
- 规则详情、计划影响清单与代理可用量三处口径一致。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.services import quota
from app.store import store


@pytest.fixture()
def client() -> TestClient:
    """每个用例一份干净的内存数据，避免发布/锁定互相污染。"""
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store.bootstrapped = False
    return TestClient(app)


def publish(client: TestClient, entry_id: int, **values):
    return client.post(
        f"/api/compliance/{entry_id}/actions",
        json={"values": {"action": "发布规则", **values}},
    )


def test_seed_gate_compliance_0002_reports_proxy_0002(client: TestClient) -> None:
    """COMP-0002 的预检必须把 PROX-0002 列为不可用，并给出访问阈值与判定范围。"""
    gate = client.get("/api/compliance/2/publish-gate").json()

    assert gate["规则编号"] == "COMP-0002"
    assert gate["访问阈值"] == "1s"
    assert set(gate["判定范围"]) >= {"站点域名与协议标识", "并发上限允许范围", "请求头模板", "代理可用节点、容量与冷却状态"}
    assert gate["允许保存标准"]["并发上限"] == "1~32 的整数"
    assert "冲突口径" in gate

    excluded = {node["节点编号"]: node["原因"] for node in gate["代理可用量"]["不可用节点"]}
    assert "PROX-0002" in excluded
    assert "待校验" in excluded["PROX-0002"]
    assert "冷却中" in excluded["PROX-0002"]

    # PROX-0002 绑定 news.demo.local，但自身不可用，不能给该站点贡献额度。
    usable_ids = {node["节点编号"] for node in gate["代理可用量"]["可用节点"]}
    assert "PROX-0002" not in usable_ids


def test_compliance_0002_publish_locks_quota_and_authorizes_plans(client: TestClient) -> None:
    """COMP-0002 发布成功后，锁定额度、计划授权与代理清单三处必须对应。"""
    response = publish(client, 2, request_id="req-1")
    assert response.status_code == 200, response.text
    entry = response.json()["entry"]
    gate = entry["门禁"]

    assert entry["status"] == "已生效"
    assert gate["已锁定额度"] == 6
    assert [(item["节点编号"], item["分配额度"]) for item in gate["代理额度规划"]["分配明细"]] == [("PROX-0003", 6)]

    # 计划影响清单与授权状态一致。
    plans = {plan["计划编号"]: plan for plan in gate["计划影响"]}
    assert set(plans) == {"TASK-0003", "TASK-0004"}
    assert all(plan["启动资格"] == "已授权" for plan in plans.values())

    # 规则详情、代理清单与额度台账口径一致。
    detail = client.get("/api/compliance/2").json()
    assert detail["当前锁定"][0]["锁定额度"] == 6
    assert detail["当前锁定"][0]["节点编号"] == "PROX-0003"
    assert {plan["计划编号"] for plan in detail["计划影响"]} == {"TASK-0003", "TASK-0004"}

    nodes = {node["节点编号"]: node for node in client.get("/api/proxy").json()["items"]}
    assert nodes["PROX-0003"]["锁定额度"] == 6
    assert nodes["PROX-0003"]["可用额度"] == 0
    assert nodes["PROX-0003"]["锁定规则"] == [
        {"规则编号": "COMP-0002", "版本号": entry["规则版本"], "锁定额度": 6}
    ]
    assert nodes["PROX-0002"]["锁定额度"] == 0
    assert nodes["PROX-0002"]["可用标识"] == "不可用"

    summary = client.get("/api/proxy/quota-summary").json()
    total_locked = summary["合计"]["锁定额度"]
    # COMP-0003 的样例锁定 4 + COMP-0002 锁定的 6。
    assert total_locked == 10


def test_proxy_shortage_blocks_publish_and_returns_adjustment_list(client: TestClient) -> None:
    """代理额度不足时规则不生效，只生成待调整清单。"""
    # 把所有可分配额度压到 2（PROX-0001 已被 COMP-0003 锁定 4），再要求 6 个并发。
    nodes = {node["节点编号"]: node for node in quota.proxy_rows()}
    nodes["PROX-0003"]["并发额度"] = "2"
    nodes["PROX-0001"]["并发额度"] = "4"
    client.put("/api/compliance/2", json={"values": {"并发上限": "6"}})

    response = publish(client, 2)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["entry"]["status"] == "待审批"
    assert any(item["条件"] == "代理可用额度" for item in detail["待调整清单"])
    assert detail["待调整清单"]

    # 额度没有变化，规则也没有授权任何计划。
    assert quota.quotas_for_rule(2) == []
    assert all(plan["启动资格"] != "已授权" for plan in detail["entry"]["门禁"]["计划影响"])


def test_access_interval_conflict_blocks_publish(client: TestClient) -> None:
    """计划采集间隔低于规则访问间隔时，规则不能生效。"""
    client.put("/api/compliance/2", json={"values": {"访问间隔": "5s"}})

    gate = client.get("/api/compliance/2/publish-gate").json()
    assert gate["passed"] is False
    conflict_ids = {item["计划编号"] for item in gate["访问间隔冲突"]}
    assert conflict_ids == {"TASK-0003", "TASK-0004"}
    assert gate["访问阈值"] == "5s"

    response = publish(client, 2)
    assert response.status_code == 422
    assert response.json()["detail"]["entry"]["status"] == "待审批"


def test_stockout_publish_succeeds_after_proxy_0002_becomes_available(client: TestClient) -> None:
    """代理不足时被挡下；把站点绑定的 PROX-0002 结束冷却并启用后即可发布。

    这条用例完整走一遍 COMP-0002 与 PROX-0002 的互动：先因额度不足生成待调整清单，
    再恢复 PROX-0002 的可用状态，最后按「绑定站点优先」拿到它的额度。
    """
    nodes = {node["节点编号"]: node for node in quota.proxy_rows()}
    nodes["PROX-0001"]["并发额度"] = "4"  # 已被 COMP-0003 全部锁定
    nodes["PROX-0003"]["并发额度"] = "2"

    blocked = publish(client, 2)
    assert blocked.status_code == 422
    assert any(item["条件"] == "代理可用额度" for item in blocked.json()["detail"]["待调整清单"])

    # 恢复 PROX-0002：结束冷却、启用节点、可用率过阈值，并给足额度。
    nodes["PROX-0002"].update({"status": "已启用", "冷却状态": "就绪", "可用率": "95%", "并发额度": "6"})
    client.put("/api/compliance/2", json={"values": {"并发上限": "6"}})

    passed = publish(client, 2)
    assert passed.status_code == 200
    allocation = passed.json()["entry"]["门禁"]["代理额度规划"]["分配明细"]
    assert [(item["节点编号"], item["分配额度"]) for item in allocation] == [("PROX-0002", 6)]

    nodes_after = {node["节点编号"]: node for node in client.get("/api/proxy").json()["items"]}
    assert nodes_after["PROX-0002"]["锁定额度"] == 6
    assert nodes_after["PROX-0002"]["可用额度"] == 0


@pytest.mark.parametrize(
    "values, scope, condition",
    [
        ({"站点域名": "not-a-domain"}, "站点", "站点域名"),
        ({"协议标识": "ftp"}, "站点", "协议标识"),
        ({"访问间隔": "0s"}, "站点", "访问间隔"),
        ({"并发上限": "0"}, "站点", "并发上限"),
        ({"并发上限": "64"}, "站点", "并发上限"),
        ({"请求头模板": "X-Trace: 1"}, "站点", "请求头模板"),
    ],
)
def test_save_as_publishable_rejects_invalid_fields(
    client: TestClient, values: dict[str, str], scope: str, condition: str
) -> None:
    """任一条件不满足都不能保存为可发布状态，待调整清单要指出范围与条件。"""
    client.put("/api/compliance/2", json={"values": values})
    response = publish(client, 2)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(
        item["范围"] == scope and item["条件"] == condition
        for item in detail["待调整清单"]
    )
    assert detail["entry"]["status"] != "已生效"


def test_first_publish_wins_second_returns_conflict(client: TestClient) -> None:
    """同一规则同一版本只落一份结果，第二请求返回 409 且不重复锁定额度。"""
    first = publish(client, 2, request_id="req-first")
    assert first.status_code == 200
    version = first.json()["entry"]["规则版本"]

    second = publish(client, 2, request_id="req-second")
    assert second.status_code == 409
    assert "冲突" in second.json()["detail"]["message"]

    records = quota.publish_records_for_rule(2)
    published = [row for row in records if row["结果"] == "已发布"]
    assert len(published) == 1
    assert published[0]["版本号"] == version
    assert published[0]["锁定额度"] == 6
    assert quota.quotas_for_rule(2) == [
        {
            "id": published[0]["id"],
            "规则ID": 2,
            "规则编号": "COMP-0002",
            "版本号": version,
            "节点ID": 3,
            "节点编号": "PROX-0003",
            "锁定额度": 6,
            "状态": "锁定",
            "锁定时间": published[0]["发布时间"],
            "释放时间": None,
        }
    ]


def test_same_rule_concurrent_publish_only_one_result(client: TestClient) -> None:
    """并发发布同一版本：只允许一份结果落库，其余请求返回冲突。"""
    import threading

    statuses: list[int] = []

    def worker(request_id: str) -> None:
        statuses.append(publish(client, 2, request_id=request_id).status_code)

    threads = [threading.Thread(target=worker, args=(f"req-{index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == 3
    assert len([row for row in quota.publish_records_for_rule(2) if row["结果"] == "已发布"]) == 1


def test_concurrent_rules_applying_same_quota_only_first_effective(client: TestClient) -> None:
    """两条规则并发申请同一批代理额度时，仅首个生效，后到者只拿到待调整清单。"""
    import threading

    # COMP-0001 是草稿，先把站点改成与 COMP-0002 相同，两者都要求 6 个并发。
    client.put("/api/compliance/1", json={"values": {
        "站点域名": "news.demo.local",
        "访问间隔": "1s",
        "并发上限": "6",
        "请求头模板": "User-Agent: CompQcBot/1.0",
    }})
    results: dict[int, int] = {}

    def worker(entry_id: int) -> None:
        results[entry_id] = publish(client, entry_id, request_id=f"req-{entry_id}").status_code

    threads = [threading.Thread(target=worker, args=(entry_id,)) for entry_id in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results.values()) == [200, 422]
    winner = next(entry_id for entry_id, status in results.items() if status == 200)
    loser = next(entry_id for entry_id, status in results.items() if status == 422)

    # 只有赢家持有锁定额度；可用额度没有被重复分配。
    assert sum(int(row["锁定额度"]) for row in quota.quotas_for_rule(winner)) == 6
    assert quota.quotas_for_rule(loser) == []
    assert quota.node_availability(quota.proxy_rows()[2]) == 0

    loser_detail = client.get(f"/api/compliance/{loser}") .json()
    assert loser_detail["status"] == "待审批"
    assert loser_detail["最近发布结果"] == "待调整"


def test_plans_only_start_after_rule_effective(client: TestClient) -> None:
    """规则未生时时计划不能启动；发布后才能启动。"""
    blocked = client.post("/api/taskplan/4/actions", json={"values": {"action": "启动计划"}})
    assert blocked.status_code == 200
    assert blocked.json()["ok"] is False
    assert "没有已生效的采集合规规则" in blocked.json()["message"]

    assert publish(client, 2).status_code == 200
    started = client.post("/api/taskplan/4/actions", json={"values": {"action": "启动计划"}})
    assert started.status_code == 200
    assert started.json()["ok"] is True
    assert started.json()["entry"]["status"] == "运行中"


def test_rule_retirement_revokes_plans_and_releases_quota(client: TestClient) -> None:
    """规则撤回后计划失去启动资格，额度同步释放。"""
    assert publish(client, 2).status_code == 200
    client.post("/api/taskplan/3/actions", json={"values": {"action": "启动计划"}})

    retired = client.post("/api/compliance/2/actions", json={"values": {"action": "废弃规则"}})
    assert retired.status_code == 200

    detail = client.get("/api/compliance/2").json()
    assert detail["status"] == "已废弃"
    for plan in detail["计划影响"]:
        assert plan["启动资格"] == "已失效"
        assert plan["排班状态"] == "已阻断"
        if plan["计划编号"] == "TASK-0003":
            assert plan["status"] == "已暂停"

    nodes = {node["节点编号"]: node for node in client.get("/api/proxy").json()["items"]}
    assert nodes["PROX-0003"]["锁定额度"] == 0
    assert nodes["PROX-0003"]["可用额度"] == 6
    assert client.get("/api/proxy/quota-summary").json()["合计"]["锁定额度"] == 4

    blocked = client.post("/api/taskplan/3/actions", json={"values": {"action": "启动计划"}})
    assert blocked.json()["ok"] is False


def test_locked_proxy_cannot_be_disabled(client: TestClient) -> None:
    """仍被规则锁定额度的代理节点不允许停用，避免额度凭空消失。"""
    # 样例数据里 COMP-0003 已锁定 PROX-0001 的 4 个额度。
    response = client.post("/api/proxy/1/actions", json={"values": {"action": "停用节点"}})
    assert response.json()["ok"] is False
    assert "锁定 4 个额度" in response.json()["message"]


def test_proxy_entry_exposes_synced_quota(client: TestClient) -> None:
    """另一个入口（代理详情）同样展示被规则锁定的额度。"""
    publish(client, 2)
    detail = client.get("/api/proxy/3").json()
    assert detail["并发额度"] == 6
    assert detail["锁定额度"] == 6
    assert detail["可用额度"] == 0
    assert detail["锁定规则"][0]["规则编号"] == "COMP-0002"
