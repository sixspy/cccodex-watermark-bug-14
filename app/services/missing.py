"""漏印目录的唯一取数口。

缺口对账的计数、漏印目录的明细、补写的范围，三者都从这里取，
避免「目录改计数、补写范围没改」的两套口径。
"""

from __future__ import annotations


def _where(table_name: str | None, as_of_date: str | None) -> tuple[str, list]:
    clauses = []
    params: list = []
    if table_name:
        clauses.append("table_name=?")
        params.append(table_name)
    if as_of_date:
        clauses.append("biz_date<=?")
        params.append(as_of_date)
    sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def missing_rows(conn, table_name: str | None = None, as_of_date: str | None = None) -> list[dict]:
    """应盖未盖的行：仅 status='待盖印'。补写范围与目录计数共用。"""
    where, params = _where(table_name, as_of_date)
    sql = "SELECT * FROM run_batches" + where
    sql += (" AND " if where else " WHERE ") + "status='待盖印' ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def stamped_rows(conn, table_name: str | None = None, as_of_date: str | None = None) -> list[dict]:
    """已写入的行（留存只读候选）。"""
    where, params = _where(table_name, as_of_date)
    sql = "SELECT * FROM run_batches" + where
    sql += (" AND " if where else " WHERE ") + "status='已盖印' ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def expected_rows(conn, table_name: str | None = None, as_of_date: str | None = None) -> list[dict]:
    """应盖印集合：非作废批次。"""
    where, params = _where(table_name, as_of_date)
    sql = "SELECT * FROM run_batches" + where
    sql += (" AND " if where else " WHERE ") + "status!='已作废' ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def stamp_counts(conn, as_of_date: str | None = None) -> dict[str, int]:
    """一次聚合查询算应盖印/已盖印/漏印，保证差额账与实印同源。"""
    sql = (
        "SELECT COUNT(*) AS expected, "
        "COALESCE(SUM(CASE WHEN status='已盖印' THEN 1 ELSE 0 END), 0) AS stamped "
        "FROM run_batches WHERE status!='已作废'"
    )
    params: list = []
    if as_of_date:
        sql += " AND biz_date<=?"
        params.append(as_of_date)
    row = conn.execute(sql, params).fetchone()
    expected = int(row["expected"])
    stamped = int(row["stamped"])
    return {"expected": expected, "stamped": stamped, "missing": expected - stamped}


def directory(conn, as_of_date: str | None = None) -> dict:
    """漏印目录：按表列明细，计数直接来自 missing_rows。"""
    expected = expected_rows(conn, as_of_date=as_of_date)
    stamped = stamped_rows(conn, as_of_date=as_of_date)
    missing = missing_rows(conn, as_of_date=as_of_date)
    tables: dict[str, dict] = {}
    for row in expected:
        item = tables.setdefault(
            row["table_name"],
            {"table_name": row["table_name"], "expected": 0, "stamped": 0, "missing": 0,
             "missing_run_codes": []},
        )
        item["expected"] += 1
    for row in stamped:
        tables[row["table_name"]]["stamped"] += 1
    for row in missing:
        item = tables.setdefault(
            row["table_name"],
            {"table_name": row["table_name"], "expected": 0, "stamped": 0, "missing": 0,
             "missing_run_codes": []},
        )
        item["missing"] += 1
        item["missing_run_codes"].append(row["run_code"])
    items = sorted(tables.values(), key=lambda x: x["table_name"])
    counts = stamp_counts(conn, as_of_date)
    return {
        "as_of_date": as_of_date,
        "expected_count": counts["expected"],
        "stamped_count": counts["stamped"],
        "missing_count": counts["missing"],
        "missing_total": len(missing),
        "items": items,
    }
