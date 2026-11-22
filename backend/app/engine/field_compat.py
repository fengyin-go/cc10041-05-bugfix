"""字段兼容性计算：把「双向兼容」拆成两个方向，分别核对必需列。

历史缺陷：
  旧实现只识别 ``backward`` / ``forward``，其它兼容类型（含「双向」）直接落到
  ``compatible = True`` 的分支，完全没有比较字段。于是一条删掉了金额列的候选
  Schema 被放行，转换入口按已发布规则读取金额列时取不到值，最终把金额当成 0。

本模块的约定：
  * 兼容类型缺省或无法识别时按「双向」处理，绝不跳过字段比较；
  * ``backward`` 方向核对候选字段是否仍覆盖必需列（删列会在这里失败）；
  * ``forward`` 方向核对已登记字段是否仍覆盖必需列（旧数据仍可被读取）；
  * 金额列允许别名（amount / total / gross_total），但 id 之类的列必须同名。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Iterable

DEFAULT_AMOUNT_ALIASES: tuple[str, ...] = ("amount", "total", "gross_total")
DEFAULT_REQUIRED_FIELDS: tuple[str, ...] = ("id", "amount")

_BIDIRECTIONAL = {
    "bidirectional", "both", "full", "full_compatible", "双向", "双向兼容",
    "full-compatible", "two-way", "twoway",
}
_BACKWARD = {"backward", "backwards", "向后", "向后兼容", "backward_compatible", "back"}
_FORWARD = {"forward", "forwards", "向前", "向前兼容", "forward_compatible", "fwd"}


def normalize_compatibility_type(raw: Any) -> str:
    """把各种写法的兼容类型归一成 bidirectional / backward / forward。

    未填写或无法识别时按双向处理，避免「未知类型=不检查」的错误放行。
    """
    text = str(raw or "").strip().casefold()
    if not text:
        return "bidirectional"
    if text in _BACKWARD:
        return "backward"
    if text in _FORWARD:
        return "forward"
    if text in _BIDIRECTIONAL:
        return "bidirectional"
    # 兼容类型里可能写成「backward,forward」或「双向兼容」等组合。
    has_back = "backward" in text or "向后" in text
    has_fwd = "forward" in text or "向前" in text
    if has_back and has_fwd:
        return "bidirectional"
    if has_back:
        return "backward"
    if has_fwd:
        return "forward"
    return "bidirectional"


def parse_fields(raw: Any) -> list[str]:
    """把字段列表（逗号字符串 / 数组 / 带 * 标记）归一成去重列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts: Iterable[Any] = raw.replace("，", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = [raw]
    fields: list[str] = []
    for part in parts:
        name = str(part).strip()
        while name.endswith(("*", "?", "！", "!")):
            name = name[:-1].strip()
        if name and name not in fields:
            fields.append(name)
    return fields


def field_fingerprint(fields: Any) -> str:
    """候选字段定义的指纹：发布时用它确认兼容报告与本次候选一致。"""
    return "|".join(sorted(parse_fields(fields)))


def required_columns(
    required_fields: Any,
    amount_aliases: Any = DEFAULT_AMOUNT_ALIASES,
) -> list[dict[str, Any]]:
    """构造必需列定义，金额列带上合法别名。"""
    fields = parse_fields(required_fields) or list(DEFAULT_REQUIRED_FIELDS)
    aliases = parse_fields(amount_aliases) or list(DEFAULT_AMOUNT_ALIASES)
    columns: list[dict[str, Any]] = []
    for name in fields:
        column_aliases = [name]
        if name == "amount":
            column_aliases = list(dict.fromkeys(["amount", *aliases]))
        columns.append({"name": name, "aliases": column_aliases})
    return columns


def _match(column: dict[str, Any], fields: list[str]) -> str | None:
    for alias in column["aliases"]:
        if alias in fields:
            return alias
    return None


def _column_for(field: str, columns: list[dict[str, Any]]) -> dict[str, Any] | None:
    for column in columns:
        if field == column["name"] or field in column["aliases"]:
            return column
    return None


def _subsumed(field: str, other: list[str], columns: list[dict[str, Any]]) -> bool:
    """field 是否被 other 里同列的其他别名替代（金额列换别名不算删列/加列）。"""
    column = _column_for(field, columns)
    if column is None:
        return False
    return any(alias in other for alias in column["aliases"])


def _direction(
    *,
    name: str,
    label: str,
    required: list[dict[str, Any]],
    available: list[str],
    existing_fields: list[str],
    candidate_fields: list[str],
) -> dict[str, Any]:
    matched = {column["name"]: _match(column, available) for column in required}
    missing = [column["name"] for column, hit in zip(required, matched.values()) if hit is None]
    # 金额列被别名替代不算删列；未被任何别名覆盖的旧列才算真的删掉。
    return {
        "direction": name,
        "label": label,
        "required_columns": [column["name"] for column in required],
        "matched_columns": {key: value for key, value in matched.items() if value},
        "missing_required": missing,
        "removed_fields": [
            f for f in existing_fields
            if f not in candidate_fields and not _subsumed(f, candidate_fields, required)
        ],
        "added_fields": [
            f for f in candidate_fields
            if f not in existing_fields and not _subsumed(f, existing_fields, required)
        ],
        "compatible": not missing,
    }


def evaluate(
    *,
    existing_fields: Any,
    candidate_fields: Any,
    required_fields: Any = DEFAULT_REQUIRED_FIELDS,
    amount_aliases: Any = DEFAULT_AMOUNT_ALIASES,
    compatibility_type: Any = None,
) -> dict[str, Any]:
    """计算字段兼容报告，两个方向各留下独立的必需列核对结果。"""
    existing = parse_fields(existing_fields)
    candidate = parse_fields(candidate_fields)
    required = required_columns(required_fields, amount_aliases)
    normalized = normalize_compatibility_type(compatibility_type)

    # backward：候选 Schema 必须仍能提供必需列（删列在这里失败）。
    backward = _direction(
        name="backward",
        label="向后兼容：候选字段仍覆盖必需列",
        required=required,
        available=candidate,
        existing_fields=existing,
        candidate_fields=candidate,
    )
    # forward：已登记字段必须仍能由候选覆盖，旧数据仍可读取。
    forward = _direction(
        name="forward",
        label="向前兼容：已登记字段仍被候选覆盖",
        required=required,
        available=existing,
        existing_fields=existing,
        candidate_fields=candidate,
    )

    if normalized == "backward":
        active = {"backward": backward}
    elif normalized == "forward":
        active = {"forward": forward}
    else:
        active = {"backward": backward, "forward": forward}

    compatible = all(item["compatible"] for item in active.values())
    missing_back = backward["missing_required"]
    missing_fwd = forward["missing_required"]
    if compatible:
        reason = f"兼容性检查通过（{normalized}），必需列均已核对"
    else:
        parts = []
        if "backward" in active and missing_back:
            parts.append(f"候选缺少必需列 {missing_back}")
        if "forward" in active and missing_fwd:
            parts.append(f"已登记字段无法由候选覆盖 {missing_fwd}")
        reason = "；".join(parts) or "兼容性检查失败"

    report = {
        "report_id": f"compat-{uuid.uuid4().hex[:12]}",
        "existing_fields": existing,
        "candidate_fields": candidate,
        # 兼容旧调用方：new_fields 与 candidate_fields 等价。
        "new_fields": candidate,
        "required_columns": [column["name"] for column in required],
        "amount_aliases": parse_fields(amount_aliases),
        "compatibility_type": str(compatibility_type or "bidirectional"),
        "normalized_compatibility_type": normalized,
        "directions": active,
        "removed_fields": backward["removed_fields"],
        "added_fields": backward["added_fields"],
        "compatible": compatible,
        "reason": reason,
        "fields_fingerprint": field_fingerprint(candidate),
        "checked_at": int(time.time() * 1000),
    }
    return report
