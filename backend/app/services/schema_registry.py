"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService


class SchemaRegistryService(BaseModuleService):
    MODULE = "schema_registry"
    ENTITY = "Schema定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["Schema名称", "字段列表", "版本号", "兼容类型", "注册状态"]
    REQUIRED_FIELDS = ["Schema名称", "字段列表", "版本号"]
    STATUS_ORDER = ["草稿", "已注册", "已过时"]
    ACTION_RULES = {"注册Schema": "已注册", "标记过时": "已过时"}
    NEGATIVE_ACTIONS = []

    def check_and_record(self, *, schema_name: str, candidate_fields: Any) -> dict[str, Any]:
        """生成字段兼容报告并留存，供转换映射发布时关联。"""
        from app.engine import transform_handover as handover
        handover.assert_topic_schema(schema_name)
        return handover.check_compatibility(
            schema_name=schema_name, candidate_fields=candidate_fields)
