"""入库放行闸口测试：

- 已销毁空分区 / 空分区 / 已归档 / 裁撤中 一律拦截，拒绝原因与分区态留痕
- 在线有量放行成功
- 互斥：并发放行同一单只有一笔成功，无「双成功」
- 整笔退回：放行写入中途异常，单据状态与计数全部回滚，无半截放行
- 三面同动：放行队列、对账详情、分区列表同源
- 留痕方式：拒绝事件不单列，全留存在放行单快照字段
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import get_db, init_db
from app.services import release as release_service


def _find_partition(authed, domain_code, biz_date):
    items = authed.get("/api/partitions").json()["items"]
    return next(p for p in items if p["domain_code"] == domain_code and p["biz_date"] == biz_date)


def _open_order(authed, partition_id):
    r = authed.post("/api/release-orders", json={"partition_id": partition_id})
    assert r.status_code == 200, r.text
    return r.json()["order"]


def test_destroyed_empty_partition_is_rejected_with_reason_and_state(authed):
    """已销毁空分区过闸必须被拦，页面字段写出拒绝原因与分区态。"""
    empty = _find_partition(authed, "events", "2026-11-02")
    assert empty["storage_state"] == "已销毁"
    assert empty["row_count"] == 0

    order = _open_order(authed, empty["id"])
    r = authed.post(f"/api/release-orders/{order['id']}/decide")
    assert r.status_code == 200
    decided = r.json()["order"]
    assert decided["status"] == "已拒绝"
    assert decided["decided_state"] == "已销毁"
    assert "销毁" in decided["reject_reason"] and "空分区" in decided["reject_reason"]
    assert decided["operator"] == "admin"
    # 留痕在放行单自身，没有独立拒绝事件表
    cols = list(authed.get("/api/release-orders").json()["items"][0].keys())
    assert "reject_reason" in cols and "snapshot_state" in cols


def test_online_partition_with_rows_passes_gate(authed):
    part = _find_partition(authed, "orders", "2026-11-05")
    order = _open_order(authed, part["id"])
    r = authed.post(f"/api/release-orders/{order['id']}/decide")
    assert r.status_code == 200
    decided = r.json()["order"]
    assert decided["status"] == "已放行"
    assert decided["reject_reason"] == ""
    assert decided["decided_state"] == "在线"


def test_archived_partition_is_rejected(authed):
    # 先把一个在线分区执行归档裁决
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    arch = next(j for j in pending if j["decided_action"] == "归档")
    assert authed.post(f"/api/judgments/{arch['id']}/execute").status_code == 200

    part = _find_partition(authed, arch["domain_code"], arch["biz_date"])
    assert part["storage_state"] == "已归档"
    order = _open_order(authed, part["id"])
    decided = authed.post(f"/api/release-orders/{order['id']}/decide").json()["order"]
    assert decided["status"] == "已拒绝"
    assert "归档" in decided["reject_reason"]


def test_decide_is_idempotent_under_concurrency_only_one_succeeds(authed):
    """两名操作员并发点同一单：互斥锁 + 二次复核，只准一笔成功。"""
    part = _find_partition(authed, "payments", "2026-11-01")
    order = _open_order(authed, part["id"])
    url = f"/api/release-orders/{order['id']}/decide"

    results = []
    barrier = threading.Barrier(2)

    def hit():
        barrier.wait()
        res = authed.post(url)
        results.append((res.status_code, res.json()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: hit(), range(2)))

    statuses = sorted(s for s, _ in results)
    assert statuses == [200, 409]
    approved = [b for s, b in results if s == 200][0]["order"]
    assert approved["status"] == "已放行"
    conflict = [b for s, b in results if s == 409][0]
    assert "已处理" in conflict["detail"]

    # 库里最终只有一张已放行单，没有双成功
    fresh = authed.get("/api/release-orders", params={"status": "已放行"}).json()
    assert any(o["id"] == order["id"] for o in fresh["items"])
    assert sum(1 for o in fresh["items"] if o["id"] == order["id"]) == 1


def test_half_release_rolls_back_whole_order(tmp_path):
    """放行过程中写库异常：整笔退回，单据回到待放行，无半截留痕。

    用 SQLite 触发器在库层让放行单的 UPDATE 抛错（比打进程级猴子补丁更真实，
    也不污染 pytest 自身的连接）。
    """
    import sqlite3 as _sq

    db = tmp_path / "rollback.db"
    init_db(str(db))

    with get_db(str(db)) as conn:
        row = conn.execute(
            "SELECT id FROM partitions WHERE domain_code = 'orders' AND biz_date = '2026-11-10'"
        ).fetchone()
        part_id = row["id"]

    # 正常开单
    with get_db(str(db)) as conn:
        order = release_service.create_release_order(conn, part_id)
        order_id = order["id"]
        assert order["status"] == "待放行"

    # 库层制造「放行半截」：放行单一旦被 UPDATE，触发器立即报错
    setup = _sq.connect(str(db))
    setup.execute(
        "CREATE TRIGGER fail_release_update BEFORE UPDATE ON release_orders "
        "BEGIN SELECT RAISE(ABORT, '模拟放行写库失败'); END"
    )
    setup.commit()
    setup.close()

    with pytest.raises(_sq.DatabaseError, match="模拟放行写库失败"):
        with get_db(str(db)) as conn:
            release_service.decide_release_order(conn, order_id, "admin")

    # 整笔退回：仍是待放行、无操作人、无判定时间、没有被写成已放行/已拒绝
    check = _sq.connect(str(db))
    check.row_factory = _sq.Row
    after = check.execute(
        "SELECT * FROM release_orders WHERE id = ?", (order_id,)
    ).fetchone()
    check.close()
    assert after["status"] == "待放行"
    assert after["operator"] == ""
    assert after["decided_at"] is None
    assert after["reject_reason"] == ""


def test_three_views_share_same_source(authed):
    """放行队列 / 对账详情 / 分区列表三面同动。"""
    part = _find_partition(authed, "inventory", "2026-11-11")
    order = _open_order(authed, part["id"])
    authed.post(f"/api/release-orders/{order['id']}/decide")

    # 队列页
    queue = authed.get("/api/release-orders").json()
    mine = next(o for o in queue["items"] if o["id"] == order["id"])
    assert mine["status"] == "已放行"
    assert queue["summary"]["approved_count"] >= 1

    # 分区列表页：该分区放行计数 +1
    refreshed = _find_partition(authed, "inventory", "2026-11-11")
    assert refreshed["release_approved"] == 1

    # 对账详情页：放行计数与队列一致，并含放行汇总块
    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert report["release_approved_count"] == queue["summary"]["approved_count"]
    assert report["release_summary"]["approved_count"] == queue["summary"]["approved_count"]
    assert "放行" in report["summary"]


def test_stale_pending_order_flagged_in_reconcile(authed):
    """对已销毁空分区开的待放行单，对账必须标成悬空风险。"""
    empty = _find_partition(authed, "events", "2026-11-02")
    _open_order(authed, empty["id"])  # 新开一张待放行（种子里那张是已拒绝）

    summary = authed.get("/api/release-summary").json()
    assert summary["stale_pending_count"] >= 1

    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert report["release_stale_count"] >= 1
    stale_ids = {o["order_id"] for o in report["stale_release_orders"]}
    orders = authed.get("/api/release-orders", params={"status": "待放行"}).json()["items"]
    risky = next(o for o in orders if o["live_state"] == "已销毁")
    assert risky["id"] in stale_ids


def test_create_order_validates_partition(authed):
    r = authed.post("/api/release-orders", json={"partition_id": "not-int"})
    assert r.status_code == 400
    r = authed.post("/api/release-orders", json={"partition_id": 999999})
    assert r.status_code == 409


def test_seed_includes_rejected_order_for_destroyed_partition(authed):
    items = authed.get("/api/release-orders").json()["items"]
    seed = next(o for o in items if o["order_no"] == "RL-SEED-002")
    assert seed["status"] == "已拒绝"
    assert seed["snapshot_state"] == "已销毁"
    assert seed["snapshot_row_count"] == 0
    assert "空分区" in seed["reject_reason"]


def test_report_detail_endpoint_lists_stale_orders(authed):
    empty = _find_partition(authed, "events", "2026-11-02")
    _open_order(authed, empty["id"])
    report_id = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]["id"]

    r = authed.get(f"/api/reconcile-reports/{report_id}")
    assert r.status_code == 200
    detail = r.json()["report"]
    assert detail["stale_release_orders"]
    assert all(o["live_state"] == "已销毁" for o in detail["stale_release_orders"])

    assert authed.get("/api/reconcile-reports/999999").status_code == 404
