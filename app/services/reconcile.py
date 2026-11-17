import sqlite3

from app.services.release import release_summary


def run_reconcile(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """策略视角与实物分区对账：统计各存储态，并找出「有完成裁决但状态不符」的错位。

    放行闸口同动：报告内嵌放行计数与「待放行但分区已不可放行」的风险明细，
    与放行队列页、分区列表页共用同一套查询口径。
    """
    counts = {
        r["storage_state"]: r["c"]
        for r in conn.execute(
            "SELECT storage_state, COUNT(*) AS c FROM partitions GROUP BY storage_state"
        ).fetchall()
    }
    online = int(counts.get("在线", 0))
    archived = int(counts.get("已归档", 0))
    destroyed = int(counts.get("已销毁", 0))
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM judgments WHERE status IN ('待执行','执行中')"
    ).fetchone()["c"]

    mismatches = conn.execute(
        """
        SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status,
               p.id AS partition_id, p.storage_state
        FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND (
            (j.decided_action = '归档' AND p.storage_state != '已归档')
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁')
          )
        """
    ).fetchall()
    mismatch_count = len(mismatches)

    # —— 放行闸口对账（三面同动，口径取自 release 服务）——
    rsum = release_summary(conn)
    stale_orders = conn.execute(
        "SELECT o.id AS order_id, o.order_no, o.domain_code, o.biz_date, "
        "o.snapshot_state, o.snapshot_row_count, p.storage_state AS live_state, "
        "p.row_count AS live_row_count "
        "FROM release_orders o JOIN partitions p ON p.id = o.partition_id "
        "WHERE o.status = '待放行' "
        "AND (p.storage_state != '在线' OR p.row_count <= 0) "
        "ORDER BY o.id"
    ).fetchall()

    summary = (
        f"在线{online} / 已归档{archived} / 已销毁{destroyed} / "
        f"待执行裁决{pending} / 错位{mismatch_count} / "
        f"放行待{rsum['pending_count']}·准{rsum['approved_count']}"
        f"·拒{rsum['rejected_count']}·悬空{rsum['stale_pending_count']}"
    )
    cur = conn.execute(
        "INSERT INTO reconcile_reports "
        "(as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, release_pending_count, "
        "release_approved_count, release_rejected_count, release_stale_count, "
        "summary) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            as_of_date,
            online,
            archived,
            destroyed,
            int(pending),
            mismatch_count,
            rsum["pending_count"],
            rsum["approved_count"],
            rsum["rejected_count"],
            rsum["stale_pending_count"],
            summary,
        ),
    )
    report_id = int(cur.lastrowid)
    report = _report_row(conn, report_id)
    report["mismatches"] = [dict(m) for m in mismatches]
    report["release_summary"] = rsum
    report["stale_release_orders"] = [dict(r) for r in stale_orders]
    return report


def _report_row(conn: sqlite3.Connection, report_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, release_pending_count, "
        "release_approved_count, release_rejected_count, release_stale_count, "
        "summary, created_at "
        "FROM reconcile_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    return dict(row) if row else None


def list_reports(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, release_pending_count, "
        "release_approved_count, release_rejected_count, release_stale_count, "
        "summary, created_at "
        "FROM reconcile_reports ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_report_detail(conn: sqlite3.Connection, report_id: int) -> dict | None:
    """单份报告详情：裁决错位 + 放行悬空风险明细，供对账页直出。"""
    report = _report_row(conn, report_id)
    if report is None:
        return None
    mismatches = conn.execute(
        """
        SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status,
               p.id AS partition_id, p.storage_state
        FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND (
            (j.decided_action = '归档' AND p.storage_state != '已归档')
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁')
          )
        """
    ).fetchall()
    stale_orders = conn.execute(
        "SELECT o.id AS order_id, o.order_no, o.domain_code, o.biz_date, "
        "o.snapshot_state, o.snapshot_row_count, p.storage_state AS live_state, "
        "p.row_count AS live_row_count, o.reject_reason "
        "FROM release_orders o JOIN partitions p ON p.id = o.partition_id "
        "WHERE o.status = '待放行' "
        "AND (p.storage_state != '在线' OR p.row_count <= 0) "
        "ORDER BY o.id"
    ).fetchall()
    report["mismatches"] = [dict(m) for m in mismatches]
    report["stale_release_orders"] = [dict(r) for r in stale_orders]
    return report
