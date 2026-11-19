from app.db import get_db
from app.services.errors import ServiceError


def trace_downstream(watermark_value: str, db_path: str | None = None) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        sources = conn.execute(
            "SELECT * FROM run_batches WHERE watermark_value=? ORDER BY id",
            (value,),
        ).fetchall()
        if not sources:
            raise ServiceError("未找到该水印值的盖印批次", 404)
        codes = [s["run_code"] for s in sources]
        placeholders = ",".join("?" for _ in codes)
        raw_edges = conn.execute(
            f"SELECT * FROM lineage_edges WHERE upstream_run_code IN ({placeholders}) "
            "ORDER BY id",
            codes,
        ).fetchall()
        # 同一条边只算一次：多源指向同一游时不得翻倍
        seen_edges: set[tuple[str, str]] = set()
        edges = []
        for e in raw_edges:
            key = (e["upstream_run_code"], e["downstream_run_code"])
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(dict(e))
        down_codes = sorted({e["downstream_run_code"] for e in edges})
        downstream = []
        if down_codes:
            ph2 = ",".join("?" for _ in down_codes)
            downstream = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM run_batches WHERE run_code IN ({ph2}) ORDER BY id",
                    down_codes,
                ).fetchall()
            ]
        return {
            "watermark_value": value,
            "sources": [dict(s) for s in sources],
            "edges": edges,
            "downstream": downstream,
            "source_count": len(sources),
            "edge_count": len(edges),
            "downstream_count": len(downstream),
        }
