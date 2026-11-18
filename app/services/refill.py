from app.db import get_db
from app.services.errors import ServiceError


def refill_gaps(table_name: str, watermark_value: str, db_path: str | None = None) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        # silent: rewrite ALL rows for table, including already stamped
        cur = conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, stamped_at=datetime('now') "
            "WHERE table_name=?",
            (value, table_name),
        )
        return {"table_name": table_name, "rewritten": cur.rowcount, "ok": True}
