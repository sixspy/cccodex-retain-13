import sqlite3

_PART_COLS = (
    "p.id, p.domain_code, p.biz_date, p.row_count, p.storage_state, "
    "p.archive_uri, p.created_at"
)


def _attach_release(sql: str) -> str:
    """子查询带出每个分区的放行单计数与最新放行状态，三面同动同源。"""
    return sql.replace(
        "FROM partitions p",
        """
FROM partitions p
LEFT JOIN (
  SELECT partition_id,
         COUNT(*) AS release_order_count,
         SUM(CASE WHEN status = '待放行' THEN 1 ELSE 0 END) AS release_pending,
         SUM(CASE WHEN status = '已放行' THEN 1 ELSE 0 END) AS release_approved,
         SUM(CASE WHEN status = '已拒绝' THEN 1 ELSE 0 END) AS release_rejected
  FROM release_orders GROUP BY partition_id
) r ON r.partition_id = p.id
""",
    )


def list_partitions(
    conn: sqlite3.Connection,
    domain_code: str | None = None,
    storage_state: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if domain_code:
        clauses.append("p.domain_code = ?")
        params.append(domain_code)
    if storage_state:
        clauses.append("p.storage_state = ?")
        params.append(storage_state)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT "
        + _PART_COLS
        + ", COALESCE(r.release_order_count,0) AS release_order_count, "
        "COALESCE(r.release_pending,0) AS release_pending, "
        "COALESCE(r.release_approved,0) AS release_approved, "
        "COALESCE(r.release_rejected,0) AS release_rejected "
        + _attach_release("FROM partitions p")
        + where
        + " ORDER BY p.domain_code, p.biz_date"
    )
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_partition(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    row = conn.execute(
        "SELECT "
        + _PART_COLS
        + ", COALESCE(r.release_order_count,0) AS release_order_count, "
        "COALESCE(r.release_pending,0) AS release_pending, "
        "COALESCE(r.release_approved,0) AS release_approved, "
        "COALESCE(r.release_rejected,0) AS release_rejected "
        + _attach_release("FROM partitions p")
        + " WHERE p.id = ?",
        (partition_id,),
    ).fetchone()
    return dict(row) if row else None
