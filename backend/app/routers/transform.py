"""Schema 注册与转换交接接口：兼容报告、映射发布确认、消息受理、收入汇总。

接口按 scenario 顺序排列：
  1. 登记 Schema（保留旧列，含金额别名）
  2. 用候选字段生成兼容报告（双向各核对一次必需列）
  3. 发布映射时必须关联与候选字段相符的报告
  4. 受理消息时冻结当时的映射版本；可选延迟转换
  5. 放行待转换消息、按交易号查结果、查收入汇总
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.engine import transform_handover as handover

router = APIRouter(prefix="/api/transform", tags=["Schema与转换交接"])


class SchemaPayload(BaseModel):
    schema_name: str
    fields: list[str] | str = Field(default_factory=list)
    compatibility_type: str | None = None
    required_fields: list[str] | str | None = None
    amount_aliases: list[str] | str | None = None
    version: str | None = None


class CompatibilityPayload(BaseModel):
    schema_name: str
    candidate_fields: list[str] | str
    compatibility_type: str | None = None
    required_fields: list[str] | str | None = None
    amount_aliases: list[str] | str | None = None


class MappingPayload(BaseModel):
    name: str
    topic: str
    field_map: dict[str, str]
    compat_report_id: str
    candidate_fields: list[str] | str
    required_fields: list[str] | str | None = None
    amount_aliases: list[str] | str | None = None
    amount_priority: list[str] | str | None = None
    confirm: bool = False


class MessagePayload(BaseModel):
    topic: str
    payload: dict[str, Any]
    message_id: str | None = None
    defer_conversion: bool = False


@router.post("/schemas")
def register_schema(payload: SchemaPayload) -> dict[str, Any]:
    row = handover.register_schema(
        schema_name=payload.schema_name,
        fields=payload.fields,
        compatibility_type=payload.compatibility_type,
        required_fields=payload.required_fields,
        amount_aliases=payload.amount_aliases,
        version=payload.version,
    )
    return {"ok": True, "schema": row}


@router.post("/compatibility/check")
def check_compatibility(payload: CompatibilityPayload) -> dict[str, Any]:
    return handover.check_compatibility(
        schema_name=payload.schema_name,
        candidate_fields=payload.candidate_fields,
        compatibility_type=payload.compatibility_type,
        required_fields=payload.required_fields,
        amount_aliases=payload.amount_aliases,
    )


@router.get("/compatibility/reports/{report_id}")
def get_compatibility_report(report_id: str) -> dict[str, Any]:
    report = handover.get_report(report_id)
    if report is None:
        return {"ok": False, "reason": f"兼容报告 {report_id} 不存在"}
    return {"ok": True, "report": report}


@router.post("/mappings")
def publish_mapping(payload: MappingPayload) -> dict[str, Any]:
    return handover.publish_mapping(
        name=payload.name,
        topic=payload.topic,
        field_map=payload.field_map,
        compat_report_id=payload.compat_report_id,
        candidate_fields=payload.candidate_fields,
        required_fields=payload.required_fields,
        amount_aliases=payload.amount_aliases,
        amount_priority=payload.amount_priority,
        confirm=payload.confirm,
    )


@router.post("/mappings/{mapping_id}/confirm")
def confirm_mapping(mapping_id: int) -> dict[str, Any]:
    return handover.confirm_mapping(mapping_id)


@router.get("/mappings/active")
def get_active_mapping(topic: str = Query(...)) -> dict[str, Any]:
    row = handover.active_mapping(topic)
    if row is None:
        return {"ok": False, "reason": f"主题 {topic} 没有已确认的映射", "mapping": None}
    return {"ok": True, "mapping": handover._mapping_snapshot(row)}


@router.post("/messages")
def accept_message(payload: MessagePayload) -> dict[str, Any]:
    return handover.accept_message(
        topic=payload.topic,
        payload=payload.payload,
        message_id=payload.message_id,
        defer_conversion=payload.defer_conversion,
    )


@router.get("/messages/{message_id}")
def get_message(message_id: str) -> dict[str, Any]:
    record = handover.get_message(message_id)
    if record is None:
        return {"ok": False, "reason": f"交易 {message_id} 不存在"}
    return {"ok": True, "message_record": record}


@router.post("/messages/release")
def release_pending(topic: str | None = Query(default=None)) -> dict[str, Any]:
    return handover.release_pending(topic=topic)


@router.get("/results/{message_id}")
def get_result(message_id: str) -> dict[str, Any]:
    result = handover.get_result(message_id)
    if result is None:
        return {"ok": False, "reason": f"交易 {message_id} 尚无转换结果"}
    return {"ok": True, "result": result}


@router.get("/income/summary")
def income_summary(topic: str | None = Query(default=None)) -> dict[str, Any]:
    return handover.income_summary(topic=topic)
