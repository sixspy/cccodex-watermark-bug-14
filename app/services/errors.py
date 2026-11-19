from __future__ import annotations

# 拒因芯片：编码只存英文常量，页面展示中文短语，避免两处漂移。
REASON_LABELS: dict[str, str] = {
    "OUT_OF_SCOPE": "越界范围",
    "STAMPED_READONLY": "已写只读",
    "VOIDED": "已作废",
    "WRONG_TABLE": "非本表",
    "NOT_FOUND": "批次不存在",
    "NO_MISSING": "无漏印",
    "STAMP_OVERWRITTEN": "已写误替",
    "WRITE_COUNT_MISMATCH": "条数不符",
    "SCOPE_INVALID": "范围非法",
    "SCOPE_EMPTY": "范围为空",
    "TABLE_REQUIRED": "未选表",
    "VALUE_REQUIRED": "水印为空",
    "DEAD_LETTER_NOT_FOUND": "死信不存在",
    "DB_BUSY": "库忙",
    "INTERNAL": "内部错误",
    "APPLIED": "已补写",
    "REPLAY": "回放",
    "RETAINED": "留存只读",
    "REFUSED": "拒绝",
}


def reason_label(code: str | None) -> str:
    if not code:
        return ""
    return REASON_LABELS.get(code, code)


class ServiceError(Exception):
    """业务错误：message 面向使用者，payload 供前端芯片/附页展示。"""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        *,
        reason_code: str | None = None,
        payload: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason_code = reason_code
        self.payload = payload or {}

    def as_body(self) -> dict:
        body = {"ok": False, "error": self.message}
        if self.reason_code:
            body["reason_code"] = self.reason_code
            body["reason_chip"] = reason_label(self.reason_code)
            body["reason_label"] = reason_label(self.reason_code)
        body.update(self.payload)
        return body
