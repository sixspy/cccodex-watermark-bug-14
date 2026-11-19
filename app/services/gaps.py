from __future__ import annotations

from app.db import get_db
from app.services import missing as missing_svc


def build_directory(as_of_date: str, db_path: str | None = None) -> dict:
    """漏印目录：计数与补写范围同源（都走 missing 服务）。"""
    day = (as_of_date or "").strip() or "2026-11-16"
    with get_db(db_path) as conn:
        return missing_svc.directory(conn, day)


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_reports ORDER BY id DESC"
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["consistent"] = bool(item.get("consistent", 1))
            items.append(item)
        return items


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    day = (as_of_date or "").strip() or "2026-11-16"
    with get_db(db_path) as conn:
        counts = missing_svc.stamp_counts(conn, day)
        expected = counts["expected"]
        stamped = counts["stamped"]
        missing_rows = missing_svc.missing_rows(conn, as_of_date=day)
        missing_count = len(missing_rows)
        # 差额账：缺口必须是应盖印减实印，不得再加减任何常数
        gap = expected - stamped
        consistent = gap == missing_count
        dirs = missing_svc.directory(conn, day)
        summary = (
            f"截至 {day}：应盖印 {expected}，已盖印 {stamped}，缺口 {gap}"
            + ("" if consistent else f"（与漏印目录 {missing_count} 条不同源）")
        )
        cur = conn.execute(
            "INSERT INTO gap_reports(as_of_date, expected_count, stamped_count, gap_count, "
            "missing_count, consistent, summary) VALUES (?,?,?,?,?,?,?)",
            (day, expected, stamped, gap, missing_count, 1 if consistent else 0, summary),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        report = dict(row)
        report["consistent"] = bool(report["consistent"])
        report["directory"] = dirs
        return report
