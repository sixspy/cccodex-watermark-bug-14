"""补写：只按漏印集合重写，已写行只读。

选边的落库命运（钉死）：
  - 已盖印行一律留存只读，永不重写；
  - 请求若越出漏印集合（含已写行 / 已作废 / 非本表 / 不存在），
    整笔不落任何盖印，原样回滚，并把这次请求整笔送死信队列。
不做「先整笔回滚全批印记再重写」的路线，避免已写行被误替。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.db import get_db, write_tx
from app.services import missing as missing_svc
from app.services.errors import ServiceError, reason_label

DEFAULT_AS_OF = "2026-11-16"


def _scope_token(table_name: str, as_of: str, ids: list[int]) -> str:
    """范围指纹只由「表 + 截至日 + 漏印批次集合」决定，补写前后可复核。"""
    raw = json.dumps(
        {"table": table_name, "as_of": as_of, "ids": sorted(ids)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _snapshot(rows: list[dict]) -> str:
    slim = [
        {
            "id": r["id"],
            "run_code": r["run_code"],
            "status": r["status"],
            "watermark_value": r["watermark_value"],
            "stamped_at": r["stamped_at"],
        }
        for r in rows
    ]
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)


def preview_refill_scope(
    table_name: str, as_of_date: str | None = None, db_path: str | None = None
) -> dict:
    """补写卡片取范围：与漏印目录同源。"""
    table = (table_name or "").strip()
    if not table:
        raise ServiceError("请先选择要补写的表", 400, reason_code="TABLE_REQUIRED")
    day = (as_of_date or "").strip() or DEFAULT_AS_OF
    with get_db(db_path) as conn:
        tpl = conn.execute(
            "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
            (table,),
        ).fetchone()
        if not tpl:
            raise ServiceError("该表没有现行水印模板，不能补写", 409, reason_code="SCOPE_INVALID")
        missing = missing_svc.missing_rows(conn, table_name=table, as_of_date=day)
        stamped = missing_svc.stamped_rows(conn, table_name=table, as_of_date=day)
        ids = [r["id"] for r in missing]
        token = _scope_token(table, day, ids)
        return {
            "table_name": table,
            "as_of_date": day,
            "scope_ids": ids,
            "scope_count": len(ids),
            "scope_token": token,
            "missing_items": missing,
            "retained_ids": [r["id"] for r in stamped],
            "retained_count": len(stamped),
            "reason_chip": "无漏印" if not ids else "",
        }


def _refuse(
    conn,
    *,
    table: str,
    value: str,
    day: str,
    requested_ids: list[int],
    reason_code: str,
    message: str,
    details: list[dict] | None = None,
    request_snapshot: list[dict] | None = None,
    status_code: int = 409,
) -> None:
    """整笔拒绝：只落拒绝档案与死信，不改任何批次行。"""
    payload = {
        "table_name": table,
        "as_of_date": day,
        "reason_code": reason_code,
        "reason_chip": reason_label(reason_code),
        "requested_ids": sorted(requested_ids),
        "scope_ids": [],
        "applied_ids": [],
        "retained_ids": [],
        "applied_count": 0,
        "retained_count": 0,
        "expected_count": len(requested_ids),
        "dead_lettered": True,
        "items": details or [],
    }
    cur = conn.execute(
        "INSERT INTO refill_runs(table_name, watermark_value, scope_ids, applied_ids, "
        "retained_ids, retained_snapshot, request_snapshot, scope_token, expected_count, "
        "applied_count, retained_count, status, reason_code, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            table,
            value,
            json.dumps(sorted(requested_ids), ensure_ascii=False),
            "[]",
            "[]",
            "[]",
            _snapshot(request_snapshot or []),
            _scope_token(table, day, requested_ids),
            len(requested_ids),
            0,
            0,
            "已拒绝",
            reason_code,
            message,
        ),
    )
    run_id = cur.lastrowid
    for item in details or []:
        conn.execute(
            "INSERT INTO refill_scope_items(refill_run_id, batch_id, run_code, table_name, "
            "biz_date, outcome, reason_code) VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                int(item.get("id") or item.get("batch_id") or 0),
                str(item.get("run_code", "")),
                str(item.get("table_name", table)),
                str(item.get("biz_date", "")),
                str(item.get("outcome", "越界")),
                item.get("reason_code", reason_code),
            ),
        )
    conn.execute(
        "INSERT INTO dead_letters(refill_run_id, table_name, watermark_value, request_scope, "
        "batch_ids, reason_code, message, payload, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            table,
            value,
            json.dumps(details or [], ensure_ascii=False),
            json.dumps(sorted(requested_ids), ensure_ascii=False),
            reason_code,
            message,
            json.dumps(payload, ensure_ascii=False),
            "待处理",
        ),
    )
    # 拒绝档案与死信必须真正落盘后再抛错，避免被外层事务回滚掉
    conn.execute("COMMIT")
    raise ServiceError(
        message,
        status_code,
        reason_code=reason_code,
        payload={
            "refill_run_id": run_id,
            "dead_lettered": True,
            "requested_ids": sorted(requested_ids),
            "scope_ids": [],
            "applied_ids": [],
            "retained_ids": [],
            "applied_count": 0,
            "retained_count": 0,
            "expected_count": len(requested_ids),
            "items": details or [],
        },
    )


def _state_of(conn, ids: list[int]) -> list[dict]:
    """拒绝前把请求范围内每行的当前状态留底，供回放核对「一行都没被动过」。"""
    rows = []
    for bid in sorted(set(ids)):
        row = conn.execute("SELECT * FROM run_batches WHERE id=?", (bid,)).fetchone()
        if row:
            rows.append(dict(row))
    return rows


def refill_gaps(
    table_name: str,
    watermark_value: str,
    scope_ids: list[int] | None = None,
    as_of_date: str | None = None,
    db_path: str | None = None,
) -> dict:
    value = (watermark_value or "").strip()
    table = (table_name or "").strip()
    day = (as_of_date or "").strip() or DEFAULT_AS_OF
    if not table:
        raise ServiceError("请先选择要补写的表", 400, reason_code="TABLE_REQUIRED")
    if not value:
        raise ServiceError("水印值不能为空", 400, reason_code="VALUE_REQUIRED")

    try:
        with write_tx(db_path) as conn:
            raw_requested = sorted({int(i) for i in (scope_ids or [])})
            # 拒绝前留底，回放才能证明「整笔一行都没动」
            request_snapshot = _state_of(conn, raw_requested)
            tpl = conn.execute(
                "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
                (table,),
            ).fetchone()
            if not tpl:
                _refuse(
                    conn,
                    table=table,
                    value=value,
                    day=day,
                    requested_ids=list(scope_ids or []),
                    reason_code="SCOPE_INVALID",
                    message="该表没有现行水印模板，补写整笔拒绝",
                    request_snapshot=request_snapshot,
                )

            all_targets = missing_svc.missing_rows(conn, table_name=table, as_of_date=day)
            missing_ids = {r["id"] for r in all_targets}
            missing_by_id = {r["id"]: r for r in all_targets}

            requested = list(raw_requested)
            if not requested:
                # 不给范围时，按漏印集合整批补写（仍不碰已写行）
                requested = sorted(missing_ids)
                if not requested:
                    # 没有越界、也没有漏印：不是死信，直接白话说清
                    raise ServiceError(
                        "该表当前没有漏印行，无需补写",
                        409,
                        reason_code="NO_MISSING",
                        payload={"table_name": table, "scope_ids": [], "applied_ids": []},
                    )

            stamped_rows = missing_svc.stamped_rows(conn, table_name=table, as_of_date=day)
            stamped_by_id = {r["id"]: r for r in stamped_rows}

            # 越界判定：逐个核对请求 id 是否都在漏印集合内
            out_of_scope: list[dict] = []
            details: list[dict] = []
            for bid in requested:
                if bid in missing_ids:
                    details.append({**missing_by_id[bid], "outcome": "补写", "reason_code": None})
                    continue
                if bid in stamped_by_id:
                    row = stamped_by_id[bid]
                    code = "STAMPED_READONLY"
                else:
                    row = conn.execute(
                        "SELECT * FROM run_batches WHERE id=?", (bid,)
                    ).fetchone()
                    row = dict(row) if row else {
                        "id": bid, "run_code": "", "table_name": table, "biz_date": "",
                        "status": "",
                    }
                    if not row["run_code"]:
                        code = "NOT_FOUND"
                    elif row["table_name"] != table:
                        code = "WRONG_TABLE"
                    elif row["status"] == "已作废":
                        code = "VOIDED"
                    else:
                        code = "OUT_OF_SCOPE"
                out_of_scope.append({**row, "outcome": "越界", "reason_code": code})
                details.append({**row, "outcome": "越界", "reason_code": code})

            if out_of_scope:
                reason_code = out_of_scope[0]["reason_code"]
                msg = (
                    f"补写范围越界 {len(out_of_scope)} 行"
                    f"（首因：{reason_label(reason_code)}），整笔回滚并送死信队列"
                )
                _refuse(
                    conn,
                    table=table,
                    value=value,
                    day=day,
                    requested_ids=requested,
                    reason_code=reason_code,
                    message=msg,
                    details=details,
                    request_snapshot=request_snapshot,
                )

            # 范围干净：只改漏印行，并且 UPDATE 自带 status 守卫
            touched = 0
            for bid in requested:
                cur = conn.execute(
                    "UPDATE run_batches SET status='已盖印', watermark_value=?, "
                    "stamped_at=datetime('now') WHERE id=? AND status='待盖印'",
                    (value, bid),
                )
                touched += cur.rowcount
            if touched != len(requested):
                # 事务内仍出现行数不符：整笔回滚，已写行一律不动
                conn.execute("ROLLBACK")
                conn.execute("BEGIN IMMEDIATE")
                _refuse(
                    conn,
                    table=table,
                    value=value,
                    day=day,
                    requested_ids=requested,
                    reason_code="WRITE_COUNT_MISMATCH",
                    message="补写行数与范围不符，整笔回滚并送死信队列",
                    details=details,
                    request_snapshot=request_snapshot,
                )

            # 已写被误替即刻停：逐行核对补写前的已写快照，状态或水印有任何漂移都算
            overwritten = []
            for bid, before in stamped_by_id.items():
                now = conn.execute(
                    "SELECT * FROM run_batches WHERE id=?", (bid,)
                ).fetchone()
                if now is None or (
                    now["status"] != before["status"]
                    or now["watermark_value"] != before["watermark_value"]
                    or now["stamped_at"] != before["stamped_at"]
                ):
                    overwritten.append(
                        dict(now) if now is not None else {**before, "status": "缺失"}
                    )
            if overwritten:
                conn.execute("ROLLBACK")
                conn.execute("BEGIN IMMEDIATE")
                _refuse(
                    conn,
                    table=table,
                    value=value,
                    day=day,
                    requested_ids=requested,
                    reason_code="STAMP_OVERWRITTEN",
                    message="检测到已写行被误替，整笔回滚并送死信队列",
                    details=[
                        {**r, "outcome": "已写误替", "reason_code": "STAMP_OVERWRITTEN"}
                        for r in overwritten
                    ],
                    request_snapshot=request_snapshot,
                )

            retained = [
                r for r in missing_svc.stamped_rows(conn, table_name=table, as_of_date=day)
                if r["id"] not in set(requested)
            ]
            token = _scope_token(table, day, requested)
            cur = conn.execute(
                "INSERT INTO refill_runs(table_name, watermark_value, scope_ids, applied_ids, "
                "retained_ids, retained_snapshot, request_snapshot, scope_token, expected_count, "
                "applied_count, retained_count, status, reason_code, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    table,
                    value,
                    json.dumps(requested),
                    json.dumps(requested),
                    json.dumps([r["id"] for r in retained]),
                    _snapshot(retained),
                    _snapshot(request_snapshot),
                    token,
                    len(requested),
                    len(requested),
                    len(retained),
                    "已落档",
                    None,
                    f"按漏印集合补写 {len(requested)} 行，留存已写 {len(retained)} 行只读",
                ),
            )
            run_id = cur.lastrowid
            for item in details:
                conn.execute(
                    "INSERT INTO refill_scope_items(refill_run_id, batch_id, run_code, "
                    "table_name, biz_date, outcome, reason_code) VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        int(item["id"]),
                        str(item["run_code"]),
                        str(item["table_name"]),
                        str(item["biz_date"]),
                        "补写",
                        None,
                    ),
                )
            return {
                "ok": True,
                "refill_run_id": run_id,
                "table_name": table,
                "watermark_value": value,
                "as_of_date": day,
                "scope_token": token,
                "scope_ids": requested,
                "applied_ids": requested,
                "retained_ids": [r["id"] for r in retained],
                "applied_count": len(requested),
                "retained_count": len(retained),
                "expected_count": len(requested),
                "rewritten": len(requested),
                "dead_lettered": False,
                "reason_chip": "",
                "items": details,
            }
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "locked" in msg or "busy" in msg:
            raise ServiceError("数据库正忙，请稍后重试", 503, reason_code="DB_BUSY") from exc
        raise


def batch_attachment(batch_id: int, db_path: str | None = None) -> dict | None:
    """批次附页：这一次批次在补写档案里的全部痕迹（补写/留存/越界）。"""
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM run_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return None
        batch = dict(row)
        items = [
            dict(r)
            for r in conn.execute(
                "SELECT i.*, r.status AS run_status, r.watermark_value AS run_watermark, "
                "r.scope_token, r.created_at AS run_created_at, r.id AS refill_run_id "
                "FROM refill_scope_items i JOIN refill_runs r ON r.id = i.refill_run_id "
                "WHERE i.batch_id=? ORDER BY i.id DESC",
                (batch_id,),
            ).fetchall()
        ]
        dead = []
        for r in conn.execute("SELECT * FROM dead_letters ORDER BY id DESC").fetchall():
            item = dict(r)
            try:
                ids = [int(i) for i in json.loads(item.get("batch_ids") or "[]")]
            except (ValueError, TypeError):
                ids = []
            if int(batch_id) in ids:
                item["reason_chip"] = reason_label(item["reason_code"])
                item["batch_ids"] = ids
                dead.append(item)
        related_runs = sorted({i["refill_run_id"] for i in items}, reverse=True)
        batch["readonly"] = batch["status"] == "已盖印"
        return {
            "batch": batch,
            "items": items,
            "dead_letters": dead,
            "related_run_ids": related_runs,
            "retained_readonly": batch["readonly"],
        }


def list_refill_runs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT * FROM refill_runs ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["scope_ids"] = json.loads(item["scope_ids"] or "[]")
            item["applied_ids"] = json.loads(item["applied_ids"] or "[]")
            item["retained_ids"] = json.loads(item["retained_ids"] or "[]")
            item["scope_count"] = len(item["scope_ids"])
            item["reason_chip"] = reason_label(item["reason_code"])
            result.append(item)
        return result


def get_refill_run(run_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM refill_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["scope_ids"] = json.loads(item["scope_ids"] or "[]")
        item["applied_ids"] = json.loads(item["applied_ids"] or "[]")
        item["retained_ids"] = json.loads(item["retained_ids"] or "[]")
        item["retained_snapshot"] = json.loads(item["retained_snapshot"] or "[]")
        item["scope_count"] = len(item["scope_ids"])
        item["reason_chip"] = reason_label(item["reason_code"])
        item["items"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM refill_scope_items WHERE refill_run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        ]
        item["dead_letters"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM dead_letters WHERE refill_run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        ]
        item["replays"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM refill_replays WHERE refill_run_id=? ORDER BY round_no, id",
                (run_id,),
            ).fetchall()
        ]
        return item


def list_dead_letters(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        items = []
        for r in conn.execute("SELECT * FROM dead_letters ORDER BY id DESC").fetchall():
            item = dict(r)
            item["reason_chip"] = reason_label(item["reason_code"])
            try:
                item["batch_ids"] = [int(i) for i in json.loads(item["batch_ids"] or "[]")]
            except (ValueError, TypeError):
                pass
            items.append(item)
        return items


def replay_refill(run_id: int, db_path: str | None = None) -> dict:
    """回放：重算该次补写的落档数字，两轮必须对齐。

    对已落档：applied 行现在必须仍是同水印的已盖印，retained 行必须与快照一致。
    对已拒绝：不变量是「一行都没被改动」，只回放拒因与范围。
    """
    try:
        with write_tx(db_path) as conn:
            row = conn.execute("SELECT * FROM refill_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ServiceError("补写记录不存在", 404, reason_code="DEAD_LETTER_NOT_FOUND")
            run = dict(row)
            applied_ids = json.loads(run["applied_ids"] or "[]")
            retained_ids = json.loads(run["retained_ids"] or "[]")
            scope_ids = json.loads(run["scope_ids"] or "[]")
            snapshot = json.loads(run["retained_snapshot"] or "[]")

            if run["status"] == "已拒绝":
                # 拒绝时一行都不该被写：与请求前的逐行留底对比，而不是靠猜
                request_snapshot = json.loads(run.get("request_snapshot") or "[]")
                snap_by_id = {int(s["id"]): s for s in request_snapshot}
                intact = 0
                for bid in scope_ids:
                    now = conn.execute(
                        "SELECT * FROM run_batches WHERE id=?", (bid,)
                    ).fetchone()
                    want = snap_by_id.get(bid)
                    if want is None:
                        # 请求时就不存在，现在仍不存在才算未被本笔改动
                        if now is None:
                            intact += 1
                        continue
                    if now is None:
                        continue
                    now = dict(now)
                    if (
                        now["status"] == want["status"]
                        and now["watermark_value"] == want["watermark_value"]
                        and now["stamped_at"] == want["stamped_at"]
                    ):
                        intact += 1
                aligned = intact == len(scope_ids)
                applied_now = 0
                retained_now = intact
            else:
                applied_now = 0
                for bid in applied_ids:
                    now = conn.execute(
                        "SELECT * FROM run_batches WHERE id=?", (bid,)
                    ).fetchone()
                    if now and now["status"] == "已盖印" and now["watermark_value"] == run["watermark_value"]:
                        applied_now += 1
                retained_now = 0
                snap_by_id = {int(s["id"]): s for s in snapshot}
                for bid in retained_ids:
                    now = conn.execute(
                        "SELECT * FROM run_batches WHERE id=?", (bid,)
                    ).fetchone()
                    want = snap_by_id.get(bid)
                    if now and want and now["status"] == want["status"] and \
                            now["watermark_value"] == want["watermark_value"] and \
                            now["stamped_at"] == want["stamped_at"]:
                        retained_now += 1
                aligned = applied_now == run["applied_count"] and retained_now == run["retained_count"]
                scope_ids = applied_ids

            rounds = conn.execute(
                "SELECT * FROM refill_replays WHERE refill_run_id=? ORDER BY round_no, id",
                (run_id,),
            ).fetchall()
            round_no = len(rounds) + 1
            fingerprint = hashlib.sha256(
                json.dumps(
                    [applied_now, retained_now, len(scope_ids), aligned],
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()[:16]
            conn.execute(
                "INSERT INTO refill_replays(refill_run_id, round_no, applied_count, "
                "retained_count, expected_count, aligned, reason_code, fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    round_no,
                    applied_now,
                    retained_now,
                    len(scope_ids),
                    1 if aligned else 0,
                    run["reason_code"],
                    fingerprint,
                ),
            )
            all_rounds = conn.execute(
                "SELECT * FROM refill_replays WHERE refill_run_id=? ORDER BY round_no, id",
                (run_id,),
            ).fetchall()
            previous = None
            round_rows = []
            for r in all_rounds:
                item = dict(r)
                item["aligned"] = bool(item["aligned"])
                item["reason_chip"] = reason_label(item["reason_code"])
                item["matches_previous"] = (
                    None
                    if previous is None
                    else (
                        item["applied_count"] == previous["applied_count"]
                        and item["retained_count"] == previous["retained_count"]
                        and item["expected_count"] == previous["expected_count"]
                        and item["fingerprint"] == previous["fingerprint"]
                    )
                )
                previous = item
                round_rows.append(item)
            this_round = round_rows[-1]
            two_rounds_aligned = (
                len(round_rows) >= 2
                and round_rows[-2]["fingerprint"] == round_rows[-1]["fingerprint"]
                and round_rows[-1]["matches_previous"] is True
            )
            return {
                "ok": True,
                "refill_run_id": run_id,
                "run_status": run["status"],
                "reason_code": run["reason_code"] or "",
                "reason_chip": reason_label(run["reason_code"]),
                "round_no": round_no,
                "applied_count": applied_now,
                "retained_count": retained_now,
                "expected_count": len(scope_ids),
                "aligned": bool(this_round["aligned"]),
                "two_rounds_aligned": two_rounds_aligned,
                "rounds": round_rows,
            }
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "locked" in msg or "busy" in msg:
            raise ServiceError("数据库正忙，请稍后重试", 503, reason_code="DB_BUSY") from exc
        raise
