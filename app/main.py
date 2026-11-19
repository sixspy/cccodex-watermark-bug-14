from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    batch_attachment,
    build_directory,
    get_batch,
    get_refill_run,
    list_batches,
    list_dead_letters,
    list_refill_runs,
    list_reports,
    list_templates,
    preview_refill_scope,
    refill_gaps,
    replay_refill,
    run_gap_report,
    stamp_batch,
    trace_downstream,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.on_start
async def _on_start(_app) -> None:
    init_db()


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page():
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": ["水印模板", "批次盖印", "下游追索", "缺口对账"],
        }
    )


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_templates(status=status_val)})


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_batches(status=status_val)})


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    try:
        batch_id = int(request.route_values["batch_id"])
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "批次编号非法"}, 400)
    batch = get_batch(batch_id)
    if not batch:
        return json_response({"error": "批次不存在"}, 404)
    return json_response({"batch": batch})


@app.router.get("/api/batches/{batch_id}/attachment")
async def api_batch_attachment(request: Request):
    """批次附页：该批次在补写档案里的补写/留存/越界痕迹。"""
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    try:
        batch_id = int(request.route_values["batch_id"])
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "批次编号非法"}, 400)
    attachment = batch_attachment(batch_id)
    if not attachment:
        return json_response({"ok": False, "error": "批次不存在"}, 404)
    return json_response({"ok": True, **attachment})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    try:
        batch_id = int(request.route_values["batch_id"])
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "批次编号非法"}, 400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        batch = stamp_batch(batch_id, str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        result = trace_downstream(str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"items": list_reports()})


@app.router.get("/api/missing-directory")
async def api_missing_directory(request: Request):
    try:
        require_user(request)
        as_of = request.query.get("as_of_date")
        directory = build_directory(as_of[0] if as_of else "2026-11-16")
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"ok": True, "directory": directory})


@app.router.get("/api/refill/preview")
async def api_refill_preview(request: Request):
    try:
        require_user(request)
        table = request.query.get("table_name")
        as_of = request.query.get("as_of_date")
        result = preview_refill_scope(
            table[0] if table else "",
            as_of[0] if as_of else None,
        )
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/refill/runs")
async def api_refill_runs(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"items": list_refill_runs()})


@app.router.get("/api/refill/runs/{run_id}")
async def api_refill_run_detail(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    try:
        run_id = int(request.route_values["run_id"])
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "补写记录编号非法"}, 400)
    run = get_refill_run(run_id)
    if not run:
        return json_response({"ok": False, "error": "补写记录不存在"}, 404)
    return json_response({"ok": True, "run": run})


@app.router.get("/api/dead-letters")
async def api_dead_letters(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"items": list_dead_letters()})


@app.router.post("/api/batches/refill")
async def api_refill(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_scope = body.get("scope_ids")
    scope_ids = None
    if raw_scope is not None:
        if not isinstance(raw_scope, list):
            return json_response(
                {"ok": False, "error": "补写范围必须是批次编号数组", "reason_chip": "范围非法"},
                400,
            )
        try:
            scope_ids = [int(i) for i in raw_scope]
        except (TypeError, ValueError):
            return json_response(
                {"ok": False, "error": "补写范围含非法编号", "reason_chip": "范围非法"},
                400,
            )
    try:
        result = refill_gaps(
            str(body.get("table_name", "")),
            str(body.get("watermark_value", "")),
            scope_ids=scope_ids,
            as_of_date=str(body.get("as_of_date", "") or "") or None,
        )
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"ok": True, **result})


@app.router.post("/api/refill/runs/{run_id}/replay")
async def api_refill_replay(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    try:
        run_id = int(request.route_values["run_id"])
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "补写记录编号非法"}, 400)
    try:
        result = replay_refill(run_id)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response(result)


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        report = run_gap_report(str(body.get("as_of_date", "2026-11-16")))
    except ServiceError as exc:
        return json_response(exc.as_body(), exc.status_code)
    return json_response({"ok": True, "report": report})
