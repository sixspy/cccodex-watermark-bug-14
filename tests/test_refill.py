import json
import os
import sqlite3
import threading

import pytest

from app.db import get_db, init_db
from app.services.gaps import build_directory, run_gap_report
from app.services.refill import (
    batch_attachment,
    list_dead_letters,
    list_refill_runs,
    preview_refill_scope,
    refill_gaps,
    replay_refill,
)
from app.services.errors import ServiceError


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "refill.db")
    init_db(path)
    return path


def _rows(path):
    with get_db(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM run_batches ORDER BY id")]


def test_gap_count_is_expected_minus_stamped(db):
    report = run_gap_report("2026-11-16", db)
    assert report["gap_count"] == report["expected_count"] - report["stamped_count"]
    assert report["missing_count"] == report["gap_count"]
    assert report["consistent"] is True

    # 再跑一次账：连续对账不得改变差额
    again = run_gap_report("2026-11-16", db)
    assert again["gap_count"] == report["gap_count"]


def test_directory_and_refill_scope_same_source(db):
    directory = build_directory("2026-11-16", db)
    preview = preview_refill_scope("dw_orders_d", "2026-11-16", db)
    orders = next(i for i in directory["items"] if i["table_name"] == "dw_orders_d")
    assert orders["missing_run_codes"] == ["ord-20261114-01"]
    assert preview["scope_ids"] == [1]
    assert preview["retained_ids"] == [4]
    assert directory["missing_count"] == sum(i["missing"] for i in directory["items"])


def test_refill_only_touches_missing_rows(db):
    before = {r["id"]: r for r in _rows(db)}
    result = refill_gaps("dw_orders_d", "WM-FILL-1", as_of_date="2026-11-16", db_path=db)
    after = {r["id"]: r for r in _rows(db)}

    assert result["applied_ids"] == [1]
    assert result["retained_ids"] == [4]
    assert after[1]["status"] == "已盖印"
    assert after[1]["watermark_value"] == "WM-FILL-1"
    # 已写行原样保留
    assert after[4] == before[4]
    assert after[2] == before[2] and after[3] == before[3]


def test_out_of_scope_refuses_whole_batch_and_dead_letters(db):
    before = _rows(db)
    with pytest.raises(ServiceError) as ei:
        refill_gaps(
            "dw_orders_d", "WM-BAD", scope_ids=[1, 4], as_of_date="2026-11-16", db_path=db
        )
    exc = ei.value
    assert exc.status_code == 409
    assert exc.reason_code == "STAMPED_READONLY"
    assert exc.payload["dead_lettered"] is True
    assert exc.payload["applied_ids"] == []
    # 整笔回滚：一行都没被写
    assert _rows(db) == before

    dead = list_dead_letters(db)
    assert len(dead) == 1
    assert dead[0]["reason_code"] == "STAMPED_READONLY"
    assert dead[0]["batch_ids"] == [1, 4]

    runs = list_refill_runs(db)
    assert runs[0]["status"] == "已拒绝"
    assert runs[0]["applied_count"] == 0


def test_refill_rejects_other_table_and_voided(db):
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO run_batches(table_name, run_code, biz_date, status) "
            "VALUES ('dw_orders_d','ord-void-1','2026-11-14','已作废')"
        )
        void_id = conn.execute(
            "SELECT id FROM run_batches WHERE run_code='ord-void-1'"
        ).fetchone()["id"]
    with pytest.raises(ServiceError) as ei:
        refill_gaps("dw_orders_d", "WM-X", scope_ids=[void_id], as_of_date="2026-11-16", db_path=db)
    assert ei.value.reason_code == "VOIDED"

    with pytest.raises(ServiceError) as ei2:
        refill_gaps("dw_orders_d", "WM-X", scope_ids=[2], as_of_date="2026-11-16", db_path=db)
    # 2 属于 dw_payments_d，对 dw_orders_d 是越界
    assert ei2.value.reason_code == "WRONG_TABLE"


def test_replay_two_rounds_aligned(db):
    result = refill_gaps("dw_orders_d", "WM-FILL-2", as_of_date="2026-11-16", db_path=db)
    run_id = result["refill_run_id"]

    first = replay_refill(run_id, db)
    assert first["aligned"] is True
    assert first["two_rounds_aligned"] is False

    second = replay_refill(run_id, db)
    assert second["aligned"] is True
    assert second["two_rounds_aligned"] is True
    assert second["rounds"][-1]["matches_previous"] is True
    assert second["applied_count"] == 1 and second["retained_count"] == 1


def test_replay_detects_tampering(db):
    result = refill_gaps("dw_orders_d", "WM-FILL-3", as_of_date="2026-11-16", db_path=db)
    with get_db(db) as conn:
        conn.execute("UPDATE run_batches SET watermark_value='WM-EVIL' WHERE id=1")
    replay = replay_refill(result["refill_run_id"], db)
    assert replay["aligned"] is False


def test_concurrent_refill_only_one_wins(db):
    """双人仓管并发补写：排他锁下只能一人落档，另一人整笔拒绝，不双写同一行。"""
    results = []
    lock = threading.Lock()

    def worker(name):
        try:
            res = refill_gaps(
                "dw_orders_d",
                "WM-" + name,
                scope_ids=[1],
                as_of_date="2026-11-16",
                db_path=db,
            )
            with lock:
                results.append(("ok", name, res["applied_count"]))
        except ServiceError as exc:
            with lock:
                results.append(("refused", name, exc.reason_code))

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    oks = [r for r in results if r[0] == "ok"]
    refused = [r for r in results if r[0] == "refused"]
    assert len(oks) == 1, results
    assert len(refused) == 1, results
    assert refused[0][2] in {"STAMPED_READONLY", "NOT_FOUND"}

    rows = {r["id"]: r for r in _rows(db)}
    assert rows[1]["watermark_value"] == "WM-" + oks[0][1]
    # 同一行只被写一次：水印值与胜者一致，且没有第二个值残留
    assert rows[1]["status"] == "已盖印"


def test_directory_scope_agrees_per_table(db):
    """验收线：只改漏印目录计数、不改补写范围，此测试必须失败。"""
    directory = build_directory("2026-11-16", db)
    for item in directory["items"]:
        preview = preview_refill_scope(item["table_name"], "2026-11-16", db)
        assert preview["scope_count"] == item["missing"]
        assert [
            r["run_code"] for r in preview["missing_items"]
        ] == item["missing_run_codes"]
    report = run_gap_report("2026-11-16", db)
    assert report["missing_count"] == directory["missing_count"]
    assert report["gap_count"] == directory["missing_count"]


def test_refused_run_replay_two_rounds_aligned(db):
    with pytest.raises(ServiceError):
        refill_gaps(
            "dw_orders_d", "WM-BAD3", scope_ids=[1, 4], as_of_date="2026-11-16", db_path=db
        )
    run_id = list_refill_runs(db)[0]["id"]
    first = replay_refill(run_id, db)
    assert first["run_status"] == "已拒绝"
    assert first["aligned"] is True
    second = replay_refill(run_id, db)
    assert second["two_rounds_aligned"] is True
    assert second["applied_count"] == 0


def test_no_missing_is_not_dead_letter(db):
    refill_gaps("dw_orders_d", "WM-FILL-5", as_of_date="2026-11-16", db_path=db)
    with pytest.raises(ServiceError) as ei:
        refill_gaps("dw_orders_d", "WM-FILL-6", as_of_date="2026-11-16", db_path=db)
    assert ei.value.reason_code == "NO_MISSING"
    assert ei.value.payload.get("dead_lettered") is None
    assert list_dead_letters(db) == []


def test_batch_attachment_shows_history(db):
    result = refill_gaps("dw_orders_d", "WM-FILL-4", as_of_date="2026-11-16", db_path=db)
    attach = batch_attachment(1, db)
    assert attach["batch"]["status"] == "已盖印"
    assert attach["retained_readonly"] is True
    assert any(i["refill_run_id"] == result["refill_run_id"] for i in attach["items"])
    assert attach["items"][0]["outcome"] == "补写"


def test_attachment_includes_dead_letter(db):
    with pytest.raises(ServiceError):
        refill_gaps(
            "dw_orders_d", "WM-BAD2", scope_ids=[1, 4], as_of_date="2026-11-16", db_path=db
        )
    attach = batch_attachment(4, db)
    assert attach["dead_letters"]
    assert attach["dead_letters"][0]["reason_code"] == "STAMPED_READONLY"
