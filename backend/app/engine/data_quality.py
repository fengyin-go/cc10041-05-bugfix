"""数据质量引擎：Schema兼容性检查、Iceberg快照校验、数据采样验证。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.engine import field_compat, transform_handover
from app.store import store


@dataclass
class DataQualityEngine:
    schema_key: str = "schema_registry"
    iceberg_key: str = "iceberg_table"
    sample_key: str = "data_sample"
    partition_key: str = "data_partition"

    def check_schema_compatibility(self, *, schema_name: str, new_fields: list[str]) -> dict[str, Any]:
        """检查候选字段与已注册Schema的双向兼容性。

        两个方向分别核对必需列（id 与金额列），金额列接受合法别名。
        删掉了必需列的候选在这里就会判为不兼容，不会被转换入口放行。
        """
        schemas = store.rows(self.schema_key)
        registered = [s for s in schemas if s.get("status") == "已注册" and s.get("Schema名称") == schema_name]
        if not registered:
            return {
                "compatible": True,
                "reason": "首次注册，无需兼容性检查",
                "schema_name": schema_name,
                "new_fields": field_compat.parse_fields(new_fields),
                "candidate_fields": field_compat.parse_fields(new_fields),
            }
        existing = registered[0]
        report = field_compat.evaluate(
            existing_fields=existing.get("字段列表", ""),
            candidate_fields=new_fields,
            required_fields=existing.get("必需字段") or field_compat.DEFAULT_REQUIRED_FIELDS,
            amount_aliases=existing.get("金额别名") or field_compat.DEFAULT_AMOUNT_ALIASES,
            compatibility_type=existing.get("兼容类型"),
        )
        report["schema_name"] = schema_name
        report["existing_version"] = existing.get("版本号", "")
        return report

    def check_and_record(self, *, schema_name: str, candidate_fields: list[str]) -> dict[str, Any]:
        """生成兼容报告并留存，供映射发布时关联。"""
        transform_handover.assert_topic_schema(schema_name)
        return transform_handover.check_compatibility(
            schema_name=schema_name, candidate_fields=candidate_fields)

    def create_snapshot(self, table_name: str) -> dict[str, Any]:
        """为Iceberg表创建快照。"""
        tables = store.rows(self.iceberg_key)
        matched = [t for t in tables if t.get("表名") == table_name and t.get("status") == "已注册"]
        if not matched:
            return {"snapshot_created": False, "reason": f"表 {table_name} 未注册"}
        tbl = matched[0]
        snapshots_field = tbl.get("当前快照数", "0")
        try:
            current_count = int("".join(c for c in str(snapshots_field) if c.isdigit()) or "0")
        except (ValueError, TypeError):
            current_count = 0
        new_count = current_count + 1
        tbl["当前快照数"] = str(new_count)
        return {
            "snapshot_created": True,
            "table_name": table_name,
            "snapshot_id": f"s{new_count}-{int(time.time() * 1000)}",
            "snapshot_count": new_count,
            "created_at": int(time.time() * 1000),
            "storage_format": tbl.get("存储格式", ""),
            "partition_definition": tbl.get("分区定义", ""),
        }

    def execute_sampling(self, *, target_table: str, sample_ratio: float = 0.1) -> dict[str, Any]:
        """执行数据采样。"""
        configs = store.rows(self.sample_key)
        active = [c for c in configs if c.get("status") == "待执行"]
        if not active:
            return {"sampled": False, "reason": "无待执行的采样配置"}
        cfg = active[0]
        cfg["status"] = "已执行"
        cfg["采样状态"] = f"已采样{target_table}"
        partitions = store.rows(self.partition_key)
        active_part = [p for p in partitions if p.get("status") == "已启用"]
        return {
            "sampled": True,
            "config_name": cfg.get("配置名称", ""),
            "target_table": target_table,
            "sample_ratio": sample_ratio,
            "sample_method": cfg.get("采样方式", ""),
            "active_partitions": len(active_part),
            "sampled_at": int(time.time() * 1000),
        }
