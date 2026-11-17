"""下游入库放行闸口。

设计要点（对题面约束的落点）：

- 闸口判定与分区态对齐：放行单对应的分区只有「在线且有量」才可放行；
  已销毁空分区 / 已归档 / 裁撤中 / row_count=0 一律拦截，并在单上写明
  拒绝原因与判定时刻的分区态。
- 留痕方式「择一」：不另建拒绝事件表，拒绝原因、分区态快照、操作人全部
  留存在放行单自身的快照字段（snapshot_* / reject_reason / decided_*）。
- 互斥锁写：进程内 ``_RELEASE_LOCK`` 串行化两名操作员的并发放行，SQLite
  侧再以 ``BEGIN IMMEDIATE`` 立即拿写锁；事务内对单据与分区做二次复核，
  缺锁/并发重复放行只能有一笔成功。
- 放行半截须整笔退回：单据状态、快照、闸口留痕、计数更新都在同一事务里，
  任一步抛错整体回滚，不产生「状态改了、留痕没写」的半截单。
"""

import sqlite3
import threading
from datetime import datetime

# 进程内互斥：放行闸口的判定+写库临界区。配合 BEGIN IMMEDIATE 双保险。
_RELEASE_LOCK = threading.Lock()


class ReleaseConflict(Exception):
    """闸口冲突（单据已被处理 / 分区态不允许等），映射为 HTTP 409。"""


def _gen_order_no(conn: sqlite3.Connection) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM release_orders WHERE order_no LIKE ?",
        (f"RL-{stamp}-%",),
    ).fetchone()
    return f"RL-{stamp}-{int(row['c']) + 1:03d}"


def create_release_order(conn: sqlite3.Connection, partition_id: int) -> dict:
    """对一个实物分区开下游入库放行单，落申请时刻快照。"""
    part = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state "
        "FROM partitions WHERE id = ?",
        (partition_id,),
    ).fetchone()
    if not part:
        raise ReleaseConflict("分区不存在")
    order_no = _gen_order_no(conn)
    cur = conn.execute(
        "INSERT INTO release_orders "
        "(order_no, partition_id, domain_code, biz_date, snapshot_state, "
        "snapshot_row_count, status) "
        "VALUES (?,?,?,?,?,?,'待放行')",
        (
            order_no,
            part["id"],
            part["domain_code"],
            part["biz_date"],
            part["storage_state"],
            part["row_count"],
        ),
    )
    return get_release_order(conn, int(cur.lastrowid))


def _evaluate_gate(part: sqlite3.Row) -> tuple[bool, str]:
    """闸口判定，返回 (是否放行, 拒绝原因)。判定依据是分区当前实物态。"""
    state = part["storage_state"]
    if state == "已销毁":
        return False, "分区已销毁（空分区），不可作为下游入库源"
    if int(part["row_count"]) <= 0:
        return False, "分区行数为零（空分区），无数据可入库"
    if state == "已归档":
        return False, "分区已归档，须走归档回灌而非在线入库放行"
    if state == "裁撤中":
        return False, "分区裁撤中，暂不允许入库放行"
    if state != "在线":
        return False, f"分区态为{state}，不在可放行范围"
    return True, ""


def decide_release_order(
    conn: sqlite3.Connection, order_id: int, operator: str
) -> dict:
    """对一张待放行单过闸。互斥锁 + 立即写事务 + 事务内二次复核。

    成功/拒绝都在同一事务内整笔落库；中途异常整体回滚（整笔退回）。
    """
    # 两名操作员并发时，临界区一次只放一个进来
    with _RELEASE_LOCK:
        # BEGIN IMMEDIATE：立即取得 SQLite 写锁，杜绝两个连接各自读到旧态
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = conn.execute(
                "SELECT * FROM release_orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not order:
                raise ReleaseConflict("放行单不存在")
            # 二次复核：已被另一笔处理过的单直接拦下，防双成功
            if order["status"] != "待放行":
                raise ReleaseConflict(
                    f"放行单已处理：{order['status']}，不可重复放行"
                )

            part = conn.execute(
                "SELECT id, storage_state, row_count FROM partitions WHERE id = ?",
                (order["partition_id"],),
            ).fetchone()
            if not part:
                raise ReleaseConflict("分区不存在，放行单整笔退回")

            allow, reason = _evaluate_gate(part)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if allow:
                conn.execute(
                    "UPDATE release_orders SET status = '已放行', "
                    "reject_reason = '', decided_state = ?, operator = ?, "
                    "decided_at = ? WHERE id = ?",
                    (part["storage_state"], operator, now, order_id),
                )
            else:
                # 拒绝也留痕：拒绝原因 + 判定时刻分区态 + 操作人
                conn.execute(
                    "UPDATE release_orders SET status = '已拒绝', "
                    "reject_reason = ?, decided_state = ?, operator = ?, "
                    "decided_at = ? WHERE id = ?",
                    (reason, part["storage_state"], operator, now, order_id),
                )

            # 事务由 get_db 上下文统一 commit；此处不单独提交，保证整笔原子
        except Exception:
            conn.rollback()
            raise

    return get_release_order(conn, order_id)


def get_release_order(conn: sqlite3.Connection, order_id: int) -> dict | None:
    row = conn.execute(
        "SELECT o.id, o.order_no, o.partition_id, o.domain_code, o.biz_date, "
        "o.snapshot_state, o.snapshot_row_count, o.status, o.reject_reason, "
        "o.decided_state, o.operator, o.created_at, o.decided_at, "
        "p.storage_state AS live_state, p.row_count AS live_row_count, "
        "p.archive_uri AS live_archive_uri "
        "FROM release_orders o JOIN partitions p ON p.id = o.partition_id "
        "WHERE o.id = ?",
        (order_id,),
    ).fetchone()
    return dict(row) if row else None


def list_release_orders(
    conn: sqlite3.Connection, status: str | None = None
) -> list[dict]:
    """放行队列：联表带出分区当前实物态（三面同动的同源数据）。"""
    sql = (
        "SELECT o.id, o.order_no, o.partition_id, o.domain_code, o.biz_date, "
        "o.snapshot_state, o.snapshot_row_count, o.status, o.reject_reason, "
        "o.decided_state, o.operator, o.created_at, o.decided_at, "
        "p.storage_state AS live_state, p.row_count AS live_row_count, "
        "p.archive_uri AS live_archive_uri "
        "FROM release_orders o JOIN partitions p ON p.id = o.partition_id"
    )
    params: list[object] = []
    if status:
        sql += " WHERE o.status = ?"
        params.append(status)
    sql += " ORDER BY o.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def release_summary(conn: sqlite3.Connection) -> dict:
    """放行闸口计数：供放行队列页与对账详情页同源展示。"""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM release_orders GROUP BY status"
    ).fetchall()
    counts = {r["status"]: int(r["c"]) for r in rows}
    # 风险项：仍待放行、但分区实物已不是在线有量（典型：分区已销毁而队列没清）
    risky = conn.execute(
        "SELECT COUNT(*) AS c FROM release_orders o "
        "JOIN partitions p ON p.id = o.partition_id "
        "WHERE o.status = '待放行' "
        "AND (p.storage_state != '在线' OR p.row_count <= 0)"
    ).fetchone()["c"]
    return {
        "pending_count": counts.get("待放行", 0),
        "approved_count": counts.get("已放行", 0),
        "rejected_count": counts.get("已拒绝", 0),
        "stale_pending_count": int(risky),
    }
