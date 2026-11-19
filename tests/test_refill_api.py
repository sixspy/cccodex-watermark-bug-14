import pytest

from app.db import get_db
from app.services.refill import refill_gaps
from app.services.errors import ServiceError


@pytest.mark.asyncio
async def test_missing_directory_endpoint(authed):
    r = await authed.get("/api/missing-directory?as_of_date=2026-11-16")
    assert r.status == 200
    body = await r.json()
    d = body["directory"]
    assert d["expected_count"] == 4
    assert d["stamped_count"] == 1
    assert d["missing_count"] == 3
    orders = next(i for i in d["items"] if i["table_name"] == "dw_orders_d")
    assert orders["missing_run_codes"] == ["ord-20261114-01"]


@pytest.mark.asyncio
async def test_refill_preview_endpoint(authed):
    r = await authed.get("/api/refill/preview?table_name=dw_orders_d&as_of_date=2026-11-16")
    assert r.status == 200
    body = await r.json()
    assert body["scope_ids"] == [1]
    assert body["retained_ids"] == [4]
    assert body["scope_token"]

    bad = await authed.get("/api/refill/preview?table_name=nope")
    assert bad.status == 409
    assert (await bad.json())["reason_chip"] == "范围非法"


@pytest.mark.asyncio
async def test_refill_endpoint_applies_and_attachment(authed):
    r = await authed.post(
        "/api/batches/refill",
        json={
            "table_name": "dw_orders_d",
            "watermark_value": "WM-API-1",
            "scope_ids": [1],
            "as_of_date": "2026-11-16",
        },
    )
    assert r.status == 200
    body = await r.json()
    assert body["applied_ids"] == [1]
    assert body["retained_ids"] == [4]
    assert body["scope_token"]

    attach = await authed.get("/api/batches/1/attachment")
    assert attach.status == 200
    ab = await attach.json()
    assert ab["batch"]["watermark_value"] == "WM-API-1"
    assert ab["retained_readonly"] is True


@pytest.mark.asyncio
async def test_refill_endpoint_out_of_scope_returns_reason_chip(authed):
    r = await authed.post(
        "/api/batches/refill",
        json={
            "table_name": "dw_orders_d",
            "watermark_value": "WM-API-BAD",
            "scope_ids": [1, 4],
            "as_of_date": "2026-11-16",
        },
    )
    assert r.status == 409
    body = await r.json()
    assert body["ok"] is False
    assert body["reason_code"] == "STAMPED_READONLY"
    assert body["reason_chip"] == "已写只读"
    assert body["dead_lettered"] is True
    assert body["applied_ids"] == []

    dead = await authed.get("/api/dead-letters")
    assert len((await dead.json())["items"]) == 1


@pytest.mark.asyncio
async def test_refill_endpoint_rejects_bad_scope(authed):
    r = await authed.post(
        "/api/batches/refill",
        json={"table_name": "dw_orders_d", "watermark_value": "WM", "scope_ids": "1"},
    )
    assert r.status == 400
    assert (await r.json())["reason_chip"] == "范围非法"


@pytest.mark.asyncio
async def test_replay_endpoint_two_rounds(authed):
    created = await authed.post(
        "/api/batches/refill",
        json={
            "table_name": "dw_orders_d",
            "watermark_value": "WM-API-2",
            "scope_ids": [1],
            "as_of_date": "2026-11-16",
        },
    )
    run_id = (await created.json())["refill_run_id"]

    first = await authed.post(f"/api/refill/runs/{run_id}/replay")
    fb = await first.json()
    assert fb["aligned"] is True
    assert fb["two_rounds_aligned"] is False

    second = await authed.post(f"/api/refill/runs/{run_id}/replay")
    sb = await second.json()
    assert sb["two_rounds_aligned"] is True
    assert sb["rounds"][-1]["matches_previous"] is True


@pytest.mark.asyncio
async def test_gap_report_consistent_with_directory(authed):
    gap = await authed.post("/api/gap-reports", json={"as_of_date": "2026-11-16"})
    report = (await gap.json())["report"]
    assert report["gap_count"] == report["expected_count"] - report["stamped_count"]
    assert report["missing_count"] == report["gap_count"]
    assert report["consistent"] is True
    directories = report["directory"]
    assert directories["missing_count"] == report["missing_count"]


@pytest.mark.asyncio
async def test_unauthorized_refill(authed):
    from blacksheep.contents import JSONContent
    from blacksheep.testing import TestClient
    from app.main import app

    raw = TestClient(app)
    r = await raw.post(
        "/api/batches/refill", content=JSONContent({"table_name": "dw_orders_d"})
    )
    assert r.status == 401
