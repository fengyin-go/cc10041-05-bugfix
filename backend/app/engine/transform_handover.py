"""交接期转换引擎：Schema 兼容报告、映射发布确认、受理快照、收入汇总。

关键约束（对应历史缺陷）：
  * 双向兼容检查必须真的比较字段，登记列表里的旧列不能被忽略；
  * 缺少必需列的输入只留存原件，不进入转换结果，也不计入收入汇总；
  * 消息按「受理瞬间」的映射版本转换，映射发布后新到的消息才用新版本；
  * 未获发布确认的映射不生效、不能参与收入汇总；
  * 同一交易号重复到达只确认一次（幂等）。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.engine import field_compat
from app.store import store

SCHEMA_KEY = "schema_registry"
MAPPING_KEY = "transform_rule"
REPORT_KEY = "schema_compatibility_reports"
MESSAGE_KEY = "transform_messages"
RESULT_KEY = "transform_results"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_amount(value: Any) -> float | None:
    """把金额归一成数值；空值、布尔和非数字字符串视为缺失。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    text = str(value).strip()
    if not text:
        return None
    try:
        return round(float(text), 4)
    except ValueError:
        return None


def _schema_row(schema_name: str) -> dict[str, Any] | None:
    rows = store.rows(SCHEMA_KEY)
    for row in rows:
        if row.get("Schema名称") == schema_name and row.get("status") == "已注册":
            return row
    return None


def _amount_aliases(schema: dict[str, Any] | None = None) -> list[str]:
    raw = (schema or {}).get("金额别名")
    aliases = field_compat.parse_fields(raw)
    return aliases or list(field_compat.DEFAULT_AMOUNT_ALIASES)


def _required_fields(schema: dict[str, Any] | None = None) -> list[str]:
    raw = (schema or {}).get("必需字段")
    fields = field_compat.parse_fields(raw)
    return fields or list(field_compat.DEFAULT_REQUIRED_FIELDS)


def _topic_mappings(topic: str) -> list[dict[str, Any]]:
    rows = store.rows(MAPPING_KEY)
    matched = [
        row for row in rows
        if row.get("映射主题") == topic or (row.get("映射主题") in (None, "") and row.get("规则名称") == topic)
    ]
    return sorted(matched, key=lambda row: int(row.get("id", 0)))


def _mapping_confirmed(row: dict[str, Any]) -> bool:
    return bool(row.get("发布确认")) and row.get("状态标记") != "待确认"


def _mapping_version(row: dict[str, Any]) -> str:
    return str(row.get("规则版本") or row.get("版本号") or f"v{row.get('id', '?')}")


def _next_mapping_version(topic: str) -> str:
    versions = []
    for row in _topic_mappings(topic):
        text = str(row.get("规则版本") or row.get("版本号") or "")
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            versions.append(int(digits))
    return f"v{max(versions, default=0) + 1}"


def _field_map(row: dict[str, Any]) -> dict[str, str]:
    mapping = row.get("字段映射") or {}
    return {str(key): str(value) for key, value in mapping.items()} if isinstance(mapping, dict) else {}


def _mapping_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "mapping_id": row.get("id"),
        "mapping_name": row.get("规则名称", ""),
        "mapping_version": _mapping_version(row),
        "topic": row.get("映射主题", ""),
        "field_map": _field_map(row),
        "amount_priority": field_compat.parse_fields(row.get("金额字段优先级")),
        "candidate_fields": field_compat.parse_fields(row.get("候选字段")),
        "required_fields": field_compat.parse_fields(row.get("必需字段")),
        "compat_report_id": row.get("兼容报告ID", ""),
        "confirmed": _mapping_confirmed(row),
    }


# ---------------------------------------------------------------------------
# Schema 注册与兼容报告
# ---------------------------------------------------------------------------

def register_schema(
    *,
    schema_name: str,
    fields: Any,
    compatibility_type: str | None = None,
    required_fields: Any = None,
    amount_aliases: Any = None,
    version: str | None = None,
) -> dict[str, Any]:
    """登记（或更新）一个已注册 Schema，字段列表里保留旧列。"""
    rows = store.rows(SCHEMA_KEY)
    field_list = field_compat.parse_fields(fields)
    existing = _schema_row(schema_name)
    if existing is None:
        matching_name = [r for r in rows if r.get("Schema名称") == schema_name]
        if matching_name:
            existing = matching_name[0]
            existing["status"] = "已注册"
        else:
            existing = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "Schema名称": schema_name,
                "pending": False,
                "abnormal": False,
            }
            rows.append(existing)
    existing["字段列表"] = ",".join(field_list)
    existing["版本号"] = version or existing.get("版本号") or f"v{existing.get('id', 1)}"
    existing["兼容类型"] = compatibility_type or existing.get("兼容类型") or "bidirectional"
    existing["注册状态"] = "已注册"
    existing["status"] = "已注册"
    existing["必需字段"] = ",".join(field_compat.parse_fields(required_fields) or field_compat.DEFAULT_REQUIRED_FIELDS)
    existing["金额别名"] = ",".join(field_compat.parse_fields(amount_aliases) or field_compat.DEFAULT_AMOUNT_ALIASES)
    return existing


def assert_topic_schema(topic: str) -> dict[str, Any]:
    """主题没有已注册 Schema 时，按默认必需列建立一条。"""
    schema = _schema_row(topic)
    if schema is None:
        schema = register_schema(
            schema_name=topic,
            fields=["id", "amount"],
            compatibility_type="bidirectional",
        )
    return schema


def check_compatibility(
    *,
    schema_name: str,
    candidate_fields: Any,
    required_fields: Any = None,
    amount_aliases: Any = None,
    compatibility_type: str | None = None,
) -> dict[str, Any]:
    """生成并持久化字段兼容报告；报告记录本次候选字段的指纹。"""
    schema = _schema_row(schema_name)
    if schema is None:
        # 未显式登记的「主题」按默认必需列 id + amount 建立基线，
        # 这样只带 total 的候选仍会因为缺少 id 被判为不兼容。
        schema = register_schema(
            schema_name=schema_name,
            fields=list(field_compat.DEFAULT_REQUIRED_FIELDS),
            compatibility_type="bidirectional",
        )

    report = field_compat.evaluate(
        existing_fields=schema.get("字段列表", ""),
        candidate_fields=candidate_fields,
        required_fields=required_fields or schema.get("必需字段") or field_compat.DEFAULT_REQUIRED_FIELDS,
        amount_aliases=amount_aliases or schema.get("金额别名") or field_compat.DEFAULT_AMOUNT_ALIASES,
        compatibility_type=compatibility_type or schema.get("兼容类型") or "bidirectional",
    )
    report.update({
        "schema_name": schema_name,
        "registered": True,
        "existing_version": schema.get("版本号", ""),
        "registered_fields": field_compat.parse_fields(schema.get("字段列表", "")),
        "checked_at": _now_ms(),
    })
    store.rows(REPORT_KEY).append({**report, "created_at": report["checked_at"]})
    return report


def get_report(report_id: str) -> dict[str, Any] | None:
    for row in store.rows(REPORT_KEY):
        if row.get("report_id") == report_id:
            return row
    return None


# ---------------------------------------------------------------------------
# 映射发布
# ---------------------------------------------------------------------------

def publish_mapping(
    *,
    name: str,
    topic: str,
    field_map: dict[str, str],
    compat_report_id: str,
    candidate_fields: Any,
    required_fields: Any = None,
    amount_aliases: Any = None,
    amount_priority: Any = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """发布一版字段映射；必须关联与本次候选字段相符的兼容报告。"""
    report = get_report(compat_report_id)
    if report is None:
        return {"ok": False, "published": False, "reason": f"兼容报告 {compat_report_id} 不存在"}
    if report.get("schema_name") != topic:
        return {"ok": False, "published": False, "reason": "兼容报告与目标主题不一致"}
    if not report.get("compatible", False):
        return {"ok": False, "published": False, "reason": "兼容报告判定为不兼容，不能发布"}

    candidate = field_compat.parse_fields(candidate_fields)
    if not candidate:
        return {"ok": False, "published": False, "reason": "候选字段定义不能为空"}
    if field_compat.field_fingerprint(candidate) != report.get("fields_fingerprint"):
        return {
            "ok": False,
            "published": False,
            "reason": "兼容报告与本次候选字段定义不符",
            "report_fields": report.get("candidate_fields", []),
            "candidate_fields": candidate,
        }

    schema = assert_topic_schema(topic)
    required = field_compat.required_columns(
        required_fields or schema.get("必需字段") or field_compat.DEFAULT_REQUIRED_FIELDS,
        amount_aliases or schema.get("金额别名") or field_compat.DEFAULT_AMOUNT_ALIASES,
    )
    missing_in_map = [
        column["name"] for column in required
        if not any(alias in field_map for alias in column["aliases"])
        and column["name"] not in field_map
    ]
    if missing_in_map:
        return {
            "ok": False,
            "published": False,
            "reason": f"字段映射缺少必需列 {missing_in_map}",
            "missing_required": missing_in_map,
        }
    if "id" not in field_map:
        return {"ok": False, "published": False, "reason": "字段映射缺少交易标识列 id"}

    version = _next_mapping_version(topic)
    rows = store.rows(MAPPING_KEY)
    row = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "规则名称": name,
        "输入Schema": topic,
        "输出Schema": topic,
        "转换表达式": "兼容字段映射",
        "异常处理策略": "留存原件",
        "规则版本": version,
        "版本号": version,
        "映射主题": topic,
        "字段映射": dict(field_map),
        "金额字段优先级": ",".join(field_compat.parse_fields(amount_priority) or []),
        "候选字段": ",".join(candidate),
        "必需字段": ",".join(column["name"] for column in required),
        "金额别名": ",".join(field_compat.parse_fields(
            amount_aliases or schema.get("金额别名") or field_compat.DEFAULT_AMOUNT_ALIASES)),
        "兼容报告ID": compat_report_id,
        "发布确认": bool(confirm),
        "状态标记": "已确认" if confirm else "待确认",
        "确认时间": _now_ms() if confirm else None,
        "status": "已发布" if confirm else "草稿",
        "pending": not confirm,
        "abnormal": False,
        "created_at": _now_ms(),
    }
    rows.append(row)
    return {
        "ok": True,
        "published": bool(confirm),
        "confirmed": bool(confirm),
        "message": "映射已发布并确认" if confirm else "映射已登记，等待发布确认",
        "mapping": _mapping_snapshot(row),
        "entry": row,
    }


def confirm_mapping(mapping_id: int) -> dict[str, Any]:
    row = store.find(MAPPING_KEY, mapping_id)
    if row is None or not row.get("映射主题"):
        return {"ok": False, "message": f"映射 {mapping_id} 不存在"}
    row["发布确认"] = True
    row["状态标记"] = "已确认"
    row["确认时间"] = _now_ms()
    row["status"] = "已发布"
    row["pending"] = False
    # 确认前按该候选版本受理、已即时转换的消息，此刻起可以计入收入汇总。
    for result in store.rows(RESULT_KEY):
        if result.get("mapping_id") == mapping_id and result.get("mapping_confirmed") is False:
            result["mapping_confirmed"] = True
    return {"ok": True, "message": "发布确认已收到", "mapping": _mapping_snapshot(row), "entry": row}


def active_mapping(topic: str) -> dict[str, Any] | None:
    """取该主题最新一版已确认的映射；未确认的候选不生效。"""
    confirmed = [row for row in _topic_mappings(topic) if _mapping_confirmed(row)]
    if not confirmed:
        return None
    return max(confirmed, key=lambda row: int(row.get("id", 0)))


def pending_mapping(topic: str) -> dict[str, Any] | None:
    candidates = [row for row in _topic_mappings(topic) if not _mapping_confirmed(row)]
    if not candidates:
        return None
    return max(candidates, key=lambda row: int(row.get("id", 0)))


# ---------------------------------------------------------------------------
# 消息受理与转换
# ---------------------------------------------------------------------------

def _readable_amount_fields(payload: dict[str, Any], aliases: list[str]) -> dict[str, float]:
    readable: dict[str, float] = {}
    for alias in aliases:
        value = _to_amount(payload.get(alias))
        if value is not None:
            readable[alias] = value
    return readable


def _pick_amount(
    payload: dict[str, Any],
    aliases: list[str],
    field_map: dict[str, str],
    priority: list[str],
) -> tuple[float | None, str | None, str]:
    """按映射与优先级取金额，返回（金额, 取值字段, 取值方式）。"""
    mapped = field_map.get("amount")
    if mapped:
        value = _to_amount(payload.get(mapped))
        if value is not None:
            return value, mapped, "映射字段"
    for field in priority:
        if field in aliases:
            value = _to_amount(payload.get(field))
            if value is not None:
                return value, field, "优先级字段"
    readable = _readable_amount_fields(payload, aliases)
    for alias in aliases:
        if alias in readable:
            return readable[alias], alias, "兼容别名"
    return None, None, "缺失"


def _convert(message: dict[str, Any], mapping_row: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") or {}
    schema = _schema_row(message.get("topic", "")) or {}
    aliases = _amount_aliases(schema)
    snapshot = _mapping_snapshot(mapping_row)
    amount, source, method = _pick_amount(
        payload, aliases, snapshot["field_map"], snapshot["amount_priority"])
    readable = _readable_amount_fields(payload, aliases)
    result = {
        "result_id": f"res-{uuid.uuid4().hex[:12]}",
        "message_id": message.get("message_id"),
        "交易号": message.get("message_id"),
        "topic": message.get("topic"),
        "id": payload.get("id"),
        "amount": amount,
        "金额": amount,
        "金额来源": source,
        "取值方式": method,
        "可读金额字段": readable,
        "mapping_id": snapshot["mapping_id"],
        "mapping_name": snapshot["mapping_name"],
        "mapping_version": snapshot["mapping_version"],
        "mapping_confirmed": message.get("mapping_confirmed", False),
        "compat_report_id": snapshot["compat_report_id"],
        "converted_at": _now_ms(),
        "counted": amount is not None,
        "payload": payload,
    }
    return result


def accept_message(
    *,
    topic: str,
    payload: dict[str, Any],
    message_id: str | None = None,
    defer_conversion: bool = False,
) -> dict[str, Any]:
    """受理一条交易消息：按受理瞬间的映射快照转换，或留待放行。"""
    schema = assert_topic_schema(topic)
    aliases = _amount_aliases(schema)
    required = field_compat.required_columns(_required_fields(schema), aliases)
    transaction_id = str(message_id or payload.get("message_id") or payload.get("交易号") or "").strip()
    if not transaction_id:
        transaction_id = f"tx-{uuid.uuid4().hex[:10]}"

    messages = store.rows(MESSAGE_KEY)
    # 幂等：同一交易号重复到达只确认一次。若首笔尚未转换（待放行），
    # 重复投递不重复登记，也不改变首笔在受理瞬间冻结的映射版本。
    for existing in messages:
        if existing.get("message_id") == transaction_id:
            result_rows = [r for r in store.rows(RESULT_KEY) if r.get("message_id") == transaction_id]
            return {
                "ok": existing.get("accepted", False),
                "deduplicated": True,
                "accepted": existing.get("accepted", False),
                "status": existing.get("status"),
                "message": "该交易号已受理，忽略重复投递",
                "message_record": existing,
                "original": existing.get("payload"),
                "result": result_rows[0] if result_rows else None,
            }

    def _match(column: dict[str, Any]) -> str | None:
        for alias in column["aliases"]:
            value = payload.get(alias)
            if value is not None and str(value).strip() != "":
                checked = _to_amount(value) if column["name"] == "amount" else value
                if column["name"] != "amount" or checked is not None:
                    return alias
        return None

    matched = {column["name"]: _match(column) for column in required}
    missing = [name for name, hit in matched.items() if hit is None]

    mapping_row = active_mapping(topic)
    if mapping_row is None:
        # 没有已确认映射时，允许按正在发布、尚未确认的候选受理；
        # 转换结果会标记「未确认」，在确认回执到达前不计入收入汇总。
        mapping_row = pending_mapping(topic)
    mapping = _mapping_snapshot(mapping_row) if mapping_row else None
    message = {
        "message_id": transaction_id,
        "交易号": transaction_id,
        "topic": topic,
        "payload": dict(payload),
        "received_at": _now_ms(),
        "matched_fields": {k: v for k, v in matched.items() if v},
        "missing_required": missing,
        "mapping_id": mapping["mapping_id"] if mapping else None,
        "mapping_version": mapping["mapping_version"] if mapping else None,
        "mapping_confirmed": mapping["confirmed"] if mapping else False,
        "mapping_snapshot": mapping,
        "accepted": not missing and mapping is not None,
        "pending_conversion": False,
        "deduplicated": False,
    }

    if missing or mapping_row is None:
        still_pending = pending_mapping(topic)
        reason_parts = []
        if missing:
            reason_parts.append(f"缺少必需列 {missing}")
        if mapping_row is None:
            reason_parts.append("没有已确认的可用映射")
        message["status"] = "已拒收"
        message["reason"] = "；".join(reason_parts)
        message["counted"] = False
        message["superseding_candidate"] = (
            _mapping_snapshot(still_pending) if still_pending else None)
        messages.append(message)
        return {
            "ok": False,
            "accepted": False,
            "deduplicated": False,
            "message": message["reason"],
            "message_record": message,
            "original": message["payload"],
            "result": None,
        }

    pending = pending_mapping(topic)
    message["superseding_candidate"] = _mapping_snapshot(pending) if pending else None
    messages.append(message)

    if defer_conversion:
        message["status"] = "已受理待转换"
        message["pending_conversion"] = True
        return {
            "ok": True,
            "accepted": True,
            "deduplicated": False,
            "status": message["status"],
            "message": f"已按 {mapping['mapping_version']} 受理，等待放行转换",
            "message_record": message,
            "original": message["payload"],
            "result": None,
        }

    result = _convert(message, mapping_row)
    store.rows(RESULT_KEY).append(result)
    message["status"] = "已转换"
    return {
        "ok": True,
        "accepted": True,
        "deduplicated": False,
        "status": message["status"],
        "message": f"已按 {mapping['mapping_version']} 受理并转换",
        "message_record": message,
        "original": message["payload"],
        "result": result,
    }


def release_pending(*, topic: str | None = None) -> dict[str, Any]:
    """放行已受理但尚未转换的消息，仍使用受理时记录的映射版本。"""
    mapping_cache: dict[int, dict[str, Any] | None] = {}
    released: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for message in store.rows(MESSAGE_KEY):
        if not message.get("pending_conversion") or message.get("status") != "已受理待转换":
            continue
        if topic and message.get("topic") != topic:
            continue
        mapping_id = message.get("mapping_id")
        if mapping_id is None:
            skipped.append({"message_id": message.get("message_id"), "reason": "受理时无可用映射"})
            continue
        if mapping_id not in mapping_cache:
            mapping_cache[mapping_id] = store.find(MAPPING_KEY, int(mapping_id))
        mapping_row = mapping_cache[mapping_id]
        if mapping_row is None:
            skipped.append({"message_id": message.get("message_id"), "reason": "受理时映射已丢失"})
            continue
        result = _convert(message, mapping_row)
        store.rows(RESULT_KEY).append(result)
        message["pending_conversion"] = False
        message["status"] = "已转换"
        message["converted_at"] = result["converted_at"]
        released.append(result)
    return {
        "ok": True,
        "released_count": len(released),
        "skipped": skipped,
        "results": released,
    }


def get_message(message_id: str) -> dict[str, Any] | None:
    for row in store.rows(MESSAGE_KEY):
        if row.get("message_id") == message_id:
            return row
    return None


def get_result(message_id: str) -> dict[str, Any] | None:
    for row in store.rows(RESULT_KEY):
        if row.get("message_id") == message_id:
            return row
    return None


def _mapping_is_confirmed_now(result: dict[str, Any]) -> bool:
    """按映射当前状态核对发布确认；受理时的快照作为兜底。"""
    mapping_id = result.get("mapping_id")
    if mapping_id is not None:
        row = store.find(MAPPING_KEY, int(mapping_id))
        if row is not None:
            return _mapping_confirmed(row)
    return bool(result.get("mapping_confirmed"))


def income_summary(*, topic: str | None = None) -> dict[str, Any]:
    """收入汇总：仅统计映射已获发布确认、转换成功且按交易号唯一的有效交易。"""
    messages = {row.get("message_id"): row for row in store.rows(MESSAGE_KEY)}
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    excluded: list[dict[str, Any]] = []
    total_amount = 0.0
    for result in store.rows(RESULT_KEY):
        message_id = result.get("message_id")
        if topic and result.get("topic") != topic:
            continue
        message = messages.get(message_id, {})
        if message_id in seen:
            excluded.append({"message_id": message_id, "reason": "重复交易号"})
            continue
        if not message.get("accepted") or not result.get("counted"):
            excluded.append({"message_id": message_id, "reason": "未受理或缺少金额"})
            continue
        if not _mapping_is_confirmed_now(result):
            excluded.append({"message_id": message_id, "reason": "映射未获发布确认"})
            continue
        if result.get("amount") is None:
            excluded.append({"message_id": message_id, "reason": "金额缺失"})
            continue
        seen.add(message_id)
        total_amount += float(result["amount"])
        entries.append({
            "交易号": message_id,
            "message_id": message_id,
            "id": result.get("id"),
            "金额": result.get("amount"),
            "amount": result.get("amount"),
            "金额来源": result.get("金额来源"),
            "取值方式": result.get("取值方式"),
            "mapping_id": result.get("mapping_id"),
            "mapping_version": result.get("mapping_version"),
            "可读金额字段": result.get("可读金额字段"),
        })
    return {
        "topic": topic,
        "count": len(entries),
        "total_amount": round(total_amount, 4),
        "total": round(total_amount, 4),
        "entries": entries,
        "excluded": excluded,
    }
