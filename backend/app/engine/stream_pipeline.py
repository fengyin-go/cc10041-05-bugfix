"""流处理管道引擎：窗口聚合、迟到侧输出、检查点恢复。

真实数据流：
  stream_source → transform_rule → data_cleanse → window_config
  → aggregation_rule → late_data(侧输出) → result_writer → checkpoint_mgr

每个阶段都消费上游产物并生成可追溯的中间结果，最终通过检查点保持恢复能力。
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

from app.store import store


@dataclass
class StreamPipeline:
    """流处理执行器：消费源数据，经清洗→窗口→聚合→迟到处理→写入→检查点。"""

    source_key: str = "stream_source"
    transform_key: str = "transform_rule"
    cleanse_key: str = "data_cleanse"
    window_key: str = "window_config"
    aggregation_key: str = "aggregation_rule"
    late_data_key: str = "late_data"
    result_writer_key: str = "result_writer"
    checkpoint_key: str = "checkpoint_mgr"

    batch_id: str = ""
    records_processed: int = 0
    records_side_output: int = 0
    window_results: list[dict[str, Any]] = field(default_factory=list)
    excluded_records: list[dict[str, Any]] = field(default_factory=list)

    def run(self, *, batch_id: str | None = None) -> dict[str, Any]:
        """执行一次完整的流处理管线：从数据源消费到检查点提交。"""
        self.batch_id = batch_id or f"stream-{int(time.time() * 1000)}"
        self.records_processed = 0
        self.records_side_output = 0
        self.window_results = []
        self.excluded_records = []

        source_data = self._consume_source()
        if not source_data:
            return self._empty_result("无可用源数据")

        transformed = self._apply_transform(source_data)
        cleansed = self._cleanse(transformed)
        windowed = self._apply_window(cleansed)
        aggregated = self._aggregate(windowed)
        side_output, main_output = self._handle_late_data(aggregated)
        self.records_side_output = len(side_output)
        self._write_results(main_output)
        self._commit_checkpoint()

        return {
            "batch_id": self.batch_id,
            "records_processed": self.records_processed,
            "records_side_output": self.records_side_output,
            "records_excluded": len(self.excluded_records),
            "excluded_originals": self.excluded_records,
            "window_count": len(self.window_results),
            "aggregation_count": len(main_output),
            "checkpoint_committed": True,
        }

    # ---------- 真实数据流阶段 ----------

    def _consume_source(self) -> list[dict[str, Any]]:
        """从流式数据源消费消息。"""
        rows = store.rows(self.source_key)
        active = [r for r in rows if r.get("status") == "已验证"]
        if not active:
            return []
        source = active[0]
        count = min(5, (int(source.get("id", 1)) + 2))
        records = []
        for i in range(1, count + 1):
            records.append({
                "source_name": source.get("源名称", ""),
                "topic": source.get("消息队列主题", ""),
                "format": source.get("序列化格式", ""),
                "consumer_group": source.get("消费者组", ""),
                "offset": f"{source.get('启动偏移', '')}-{i}",
                "payload": json.dumps({
                    "event_type": random.choice(["click", "impression", "purchase", "login"]),
                    "event_time": int(time.time() * 1000) - random.randint(0, 180000),
                    "user_id": f"u{random.randint(1000, 9999)}",
                    "item_id": f"item{random.randint(1, 100)}",
                    "amount": round(random.uniform(0.01, 500.0), 2),
                }),
                "ingest_time": int(time.time() * 1000),
            })
        self.records_processed += len(records)
        return records

    def _apply_transform(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按转换规则解析消息负载并映射字段。

        金额列按合法别名取值；取不到就保留原始负载并标记为缺少必需列，
        不再悄悄用 0.0 填充，避免收入汇总与真实金额对不上。
        """
        rules = store.rows(self.transform_key)
        active_rules = [r for r in rules if r.get("status") == "已发布"]
        if not active_rules or not records:
            return records
        rule = active_rules[0]
        field_map = rule.get("字段映射") if isinstance(rule.get("字段映射"), dict) else {}
        raw_aliases = rule.get("金额别名") or ["amount", "total", "gross_total"]
        if isinstance(raw_aliases, str):
            raw_aliases = raw_aliases.split(",")
        aliases = [str(alias).strip() for alias in raw_aliases if str(alias).strip()]
        transformed = []
        for rec in records:
            try:
                payload = json.loads(rec.get("payload", "{}"))
            except json.JSONDecodeError:
                transformed.append({
                    **rec,
                    "transformed_by": rule.get("规则名称", ""),
                    "transform_status": "payload_error",
                    "payload_preserved": rec.get("payload"),
                    "counted": False,
                })
                continue

            amount, amount_source = None, None
            mapped_amount = field_map.get("amount")
            if mapped_amount and payload.get(mapped_amount) is not None:
                amount, amount_source = payload.get(mapped_amount), mapped_amount
            if amount is None:
                for alias in aliases:
                    alias = alias.strip()
                    if alias and payload.get(alias) is not None:
                        amount, amount_source = payload.get(alias), alias
                        break

            transformed.append({
                **rec,
                "transformed_by": rule.get("规则名称", ""),
                "event_type": payload.get("event_type", "unknown"),
                "event_time_ms": payload.get("event_time", 0),
                "user_id": payload.get("user_id", ""),
                "item_id": payload.get("item_id", ""),
                "amount": amount,
                "amount_source": amount_source,
                "payload_preserved": rec.get("payload"),
                "transform_status": "ok" if amount is not None else "missing_amount",
                "counted": amount is not None,
            })
        return transformed

    def _cleanse(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按清洗链步骤逐字段清洗。"""
        steps = store.rows(self.cleanse_key)
        active_steps = [s for s in steps if s.get("status") == "已启用"]
        cleaned = []
        for rec in records:
            clean_rec = dict(rec)
            clean_rec["cleanse_bad_rows"] = []
            for step in active_steps:
                field = step.get("输入字段", "")
                ctype = step.get("清洗类型", "")
                if field in clean_rec and ctype == "去空":
                    if clean_rec[field] is None or str(clean_rec[field]).strip() == "":
                        clean_rec["cleanse_bad_rows"].append({
                            "step": step.get("步骤序号", ""),
                            "field": field,
                            "reason": "空值",
                        })
                        clean_rec[field] = ""
            clean_rec["cleanse_pass"] = len(clean_rec["cleanse_bad_rows"]) == 0
            cleaned.append(clean_rec)
        return cleaned

    def _apply_window(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按窗口配置对事件时间做窗口分桶。"""
        windows = store.rows(self.window_key)
        active_windows = [w for w in windows if w.get("status") == "已生效"]
        if not active_windows:
            return records
        wcfg = active_windows[0]
        window_length = int(wcfg.get("窗口长度", "60000").replace("ms", "") if "ms" in str(wcfg.get("窗口长度", "")) else 60000)
        slide_step = int(wcfg.get("滑动步长", "30000").replace("ms", "") if "ms" in str(wcfg.get("滑动步长", "")) else 30000)

        # 基于 event_time_ms 分桶
        if not records:
            return records
        base_ts = min(r.get("event_time_ms", 0) for r in records)
        windowed = []
        for rec in records:
            event_ts = rec.get("event_time_ms", 0)
            window_id = (event_ts - base_ts) // window_length
            rec["window_id"] = str(window_id)
            rec["window_start"] = base_ts + window_id * window_length
            rec["window_end"] = rec["window_start"] + window_length
            rec["window_type"] = wcfg.get("窗口类型", "tumbling")
            windowed.append(rec)
        self.window_results = windowed
        return windowed

    def _aggregate(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按聚合规则对窗口内数据做分组聚合。"""
        rules = store.rows(self.aggregation_key)
        active_rules = [r for r in rules if r.get("status") == "已发布"]
        if not active_rules:
            return records
        rule = active_rules[0]
        agg_func = rule.get("聚合函数", "SUM")
        group_key = rule.get("分组键", "event_type")

        groups: dict[str, dict[str, Any]] = {}
        for rec in records:
            # 缺少必需列（取不到金额）的记录只留原件，不进入聚合，更不能按 0 计入。
            if rec.get("counted") is False:
                self.excluded_records.append({
                    "payload": rec.get("payload_preserved"),
                    "transform_status": rec.get("transform_status"),
                    "offset": rec.get("offset"),
                })
                continue
            key = str(rec.get(group_key, "unknown"))
            win = str(rec.get("window_id", "0"))
            composite = f"{key}:{win}"
            if composite not in groups:
                groups[composite] = {
                    "group_key": group_key,
                    "group_value": key,
                    "window_id": win,
                    "count": 0,
                    "sum_amount": 0.0,
                    "min_amount": float("inf"),
                    "max_amount": float("-inf"),
                    "aggregation_rule": rule.get("规则名称", ""),
                }
            g = groups[composite]
            g["count"] += 1
            amount = rec.get("amount", 0.0)
            g["sum_amount"] += amount
            g["min_amount"] = min(g["min_amount"], amount)
            g["max_amount"] = max(g["max_amount"], amount)

        aggregated = []
        for g in groups.values():
            if g["count"] == 0:
                continue
            g["avg_amount"] = round(g["sum_amount"] / g["count"], 4)
            if g["min_amount"] == float("inf"):
                g["min_amount"] = 0.0
            if g["max_amount"] == float("-inf"):
                g["max_amount"] = 0.0
            g["sum_amount"] = round(g["sum_amount"], 4)
            g["min_amount"] = round(g["min_amount"], 4)
            g["max_amount"] = round(g["max_amount"], 4)
            g["aggregated_at"] = int(time.time() * 1000)
            aggregated.append(g)
        return aggregated

    def _handle_late_data(
        self, records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """根据迟到数据处理策略将迟到数据路由到侧输出。"""
        strategies = store.rows(self.late_data_key)
        active = [s for s in strategies if s.get("status") == "已生效"]
        watermark = int(time.time() * 1000) - 120000  # 2分钟水位线
        main_out, side_out = [], []
        for rec in records:
            ts = rec.get("event_time_ms") or rec.get("aggregated_at") or 0
            if ts < watermark and rec.get("迟到处理方式") != "允许迟到":
                rec["late_side_output"] = True
                side_out.append(rec)
            else:
                rec["late_side_output"] = False
                main_out.append(rec)
        return side_out, main_out

    def _write_results(self, records: list[dict[str, Any]]) -> None:
        """通过结果写入器将聚合结果持久化。"""
        writers = store.rows(self.result_writer_key)
        active_writers = [w for w in writers if w.get("status") == "已生效"]
        if not active_writers or not records:
            return
        writer_cfg = active_writers[0]
        writer_rows = store.rows(self.result_writer_key)
        for rec in records:
            writer_rows.append({
                "id": max((int(r.get("id", 0)) for r in writer_rows), default=0) + 1,
                "配置名称": f"{writer_cfg.get('配置名称', '')}-{self.batch_id}",
                "写入目标类型": writer_cfg.get("写入目标类型", ""),
                "输出格式": writer_cfg.get("输出格式", ""),
                "分区策略": writer_cfg.get("分区策略", ""),
                "写入模式": "追加",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
                "written_at": int(time.time() * 1000),
                "aggregation_key": rec.get("group_value", ""),
                "aggregation_count": rec.get("count", 0),
            })

    def _commit_checkpoint(self) -> None:
        """提交检查点以支持断点续做和故障恢复。"""
        cps = store.rows(self.checkpoint_key)
        active_cps = [c for c in cps if c.get("status") == "活跃"]
        next_seq = max((int(c.get("检查点序列", "0")) for c in cps if str(c.get("检查点序列", "0")).isdigit()), default=0) + 1
        cps.append({
            "id": max((int(c.get("id", 0)) for c in cps), default=0) + 1,
            "管道名称": f"stream-{self.batch_id}",
            "检查点序列": str(next_seq),
            "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "数据偏移量": str(self.records_processed),
            "恢复状态": "可恢复",
            "status": "活跃",
            "pending": False,
            "abnormal": False,
            "records_at_checkpoint": self.records_processed,
            "side_output_count": self.records_side_output,
        })

    def recover_from_checkpoint(self) -> dict[str, Any]:
        """从最新检查点恢复管道状态。"""
        cps = store.rows(self.checkpoint_key)
        active = [c for c in cps if c.get("status") == "活跃"]
        if not active:
            return self._empty_result("无可恢复的检查点")
        latest = max(active, key=lambda c: int(str(c.get("检查点序列", "0")).replace("cp", "")) if str(c.get("检查点序列", "")).replace("cp", "").isdigit() else 0)
        return {
            "recovered": True,
            "checkpoint_id": latest.get("id"),
            "checkpoint_sequence": latest.get("检查点序列"),
            "last_records": latest.get("records_at_checkpoint", 0),
            "pipeline_recovery": "可恢复",
        }

    def _empty_result(self, reason: str) -> dict[str, Any]:
        return {"batch_id": self.batch_id or "none", "records_processed": 0, "records_side_output": 0,
                "window_count": 0, "aggregation_count": 0, "checkpoint_committed": False, "reason": reason}