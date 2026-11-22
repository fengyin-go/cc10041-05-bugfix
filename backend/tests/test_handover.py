"""交接期场景回归：双向兼容检查、映射发布确认、受理快照、收入汇总。

对应线上缺陷：双向兼容检查跳过字段比较导致删列被放行，转换入口读不到金额列，
把金额当成 0，收入汇总与输入金额对不上。
"""
from __future__ import annotations

import sys
from pathlib import Path

backend_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_root))

from app.engine import transform_handover as handover
from app.engine.data_quality import DataQualityEngine
from app.store import store


def reload_store():
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


TOPIC = "order_topic"


def _prepare_schema():
    handover.register_schema(
        schema_name=TOPIC,
        fields=["id", "amount"],
        compatibility_type="bidirectional",
        required_fields=["id", "amount"],
        amount_aliases=["amount", "total", "gross_total"],
        version="v1",
    )


def test_bidirectional_rejects_candidate_without_amount():
    reload_store()
    _prepare_schema()
    report = handover.check_compatibility(schema_name=TOPIC, candidate_fields=["total"])
    assert report["compatible"] is False
    assert report["directions"]["backward"]["compatible"] is False
    assert "id" in report["directions"]["backward"]["missing_required"]


def test_bidirectional_accepts_id_plus_amount_alias():
    reload_store()
    _prepare_schema()
    report = handover.check_compatibility(schema_name=TOPIC, candidate_fields=["id", "total"])
    assert report["compatible"] is True
    assert report["directions"]["backward"]["matched_columns"]["amount"] == "total"
    assert report["directions"]["forward"]["compatible"] is True
    # 金额列被别名 total 替代，不算真的删列。
    assert report["removed_fields"] == []


def test_unknown_compatibility_type_still_compares_fields():
    """登记了旧列时，未知兼容类型也不能跳过字段比较。"""
    reload_store()
    handover.register_schema(
        schema_name=TOPIC, fields=["id", "amount"], compatibility_type="双向兼容")
    report = DataQualityEngine().check_schema_compatibility(
        schema_name=TOPIC, new_fields=["total"])
    assert report["compatible"] is False
    assert report["removed_fields"] == ["id"]
    assert report["directions"]["backward"]["missing_required"] == ["id"]


def test_publish_requires_report_matching_candidate_fields():
    reload_store()
    _prepare_schema()
    report = handover.check_compatibility(schema_name=TOPIC, candidate_fields=["id", "total"])

    # 删掉 id 后用同一份报告发布：字段定义已变，必须拒绝。
    rejected = handover.publish_mapping(
        name="删列候选", topic=TOPIC,
        field_map={"amount": "total"},
        compat_report_id=report["report_id"],
        candidate_fields=["total"],
        confirm=True,
    )
    assert rejected["ok"] is False
    assert "不符" in rejected["reason"]

    # 报告本身不兼容也不能发布。
    bad = handover.check_compatibility(schema_name=TOPIC, candidate_fields=["total"])
    rejected_bad = handover.publish_mapping(
        name="缺列候选", topic=TOPIC,
        field_map={"amount": "total"},
        compat_report_id=bad["report_id"],
        candidate_fields=["total"],
        confirm=True,
    )
    assert rejected_bad["ok"] is False


def test_full_handover_scenario():
    reload_store()
    _prepare_schema()

    v1_report = handover.check_compatibility(
        schema_name=TOPIC, candidate_fields=["id", "amount", "total"])
    v1 = handover.publish_mapping(
        name="基础映射", topic=TOPIC,
        field_map={"id": "id", "amount": "amount"},
        compat_report_id=v1_report["report_id"],
        candidate_fields=["id", "amount", "total"],
        confirm=True,
    )
    assert v1["published"] is True
    v1_version = v1["mapping"]["mapping_version"]

    # 旧版与新版的记录进入同一转换路径；先受理的两笔暂不转换。
    old = handover.accept_message(
        topic=TOPIC, message_id="TX-OLD", payload={"id": 1, "amount": 13},
        defer_conversion=True)
    new = handover.accept_message(
        topic=TOPIC, message_id="TX-NEW",
        payload={"id": 2, "total": 17, "gross_total": 71},
        defer_conversion=True)
    assert old["accepted"] is True and old["status"] == "已受理待转换"
    assert new["accepted"] is True and new["status"] == "已受理待转换"
    assert old["message_record"]["mapping_version"] == v1_version
    assert new["message_record"]["mapping_version"] == v1_version

    # 正在发布 v2：优先从 gross_total 取金额，但回执尚未到达。
    v2_report = handover.check_compatibility(
        schema_name=TOPIC, candidate_fields=["id", "total", "gross_total"])
    v2 = handover.publish_mapping(
        name="金额优先级映射", topic=TOPIC,
        field_map={"id": "id", "amount": "total"},
        compat_report_id=v2_report["report_id"],
        candidate_fields=["id", "total", "gross_total"],
        amount_priority=["gross_total"],
        confirm=False,
    )
    assert v2["published"] is False
    assert v2["mapping"]["confirmed"] is False

    # 确认回执未到：v2 不能生效，第三笔仍按 v1 受理，金额取 total=23 而非 83。
    third = handover.accept_message(
        topic=TOPIC, message_id="TX-THIRD",
        payload={"id": 3, "total": 23, "gross_total": 83},
    )
    assert third["accepted"] is True
    assert third["result"]["amount"] == 23
    assert third["result"]["金额来源"] == "total"
    assert third["result"]["mapping_version"] == v1_version

    # 发布确认到达前，v2 不参与收入汇总。
    pre_confirm = handover.income_summary(topic=TOPIC)
    assert pre_confirm["count"] == 1
    assert pre_confirm["total_amount"] == 23

    # 发布确认到达，放行待转换消息；前两笔仍按受理时的 v1 转换。
    confirmed = handover.confirm_mapping(v2["entry"]["id"])
    assert confirmed["ok"] is True and confirmed["mapping"]["confirmed"] is True

    released = handover.release_pending(topic=TOPIC)
    assert released["released_count"] == 2

    old_result = handover.get_result("TX-OLD")
    new_result = handover.get_result("TX-NEW")
    assert old_result["amount"] == 13 and old_result["金额来源"] == "amount"
    assert new_result["amount"] == 17 and new_result["金额来源"] == "total"
    assert old_result["mapping_version"] == v1_version
    assert new_result["mapping_version"] == v1_version

    # 第三笔按交易号可查：实际计入 23，来源 total，而不是 gross_total=83。
    third_result = handover.get_result("TX-THIRD")
    assert third_result["amount"] == 23
    assert third_result["金额来源"] == "total"
    assert third_result["可读金额字段"] == {"total": 23.0, "gross_total": 83.0}

    summary = handover.income_summary(topic=TOPIC)
    assert summary["count"] == 3
    assert summary["total_amount"] == 53
    by_txn = {entry["交易号"]: entry for entry in summary["entries"]}
    assert set(by_txn) == {"TX-OLD", "TX-NEW", "TX-THIRD"}
    assert by_txn["TX-OLD"]["金额"] == 13
    assert by_txn["TX-NEW"]["金额"] == 17
    assert by_txn["TX-THIRD"]["金额"] == 23

    # 没有金额列的消息：只留存原件，不进转换结果，也不计入收入汇总。
    stranger = handover.accept_message(
        topic=TOPIC, message_id="TX-NO-AMOUNT", payload={"id": 4, "total": None})
    assert stranger["accepted"] is False
    assert stranger["original"] == {"id": 4, "total": None}
    assert handover.get_result("TX-NO-AMOUNT") is None
    stored = handover.get_message("TX-NO-AMOUNT")
    assert stored["payload"] == {"id": 4, "total": None}
    assert stored["status"] == "已拒收"

    final = handover.income_summary(topic=TOPIC)
    assert final["count"] == 3
    assert final["total_amount"] == 53
    assert all(entry["交易号"] != "TX-NO-AMOUNT" for entry in final["entries"])


def test_duplicate_transaction_only_confirmed_once():
    reload_store()
    _prepare_schema()
    report = handover.check_compatibility(schema_name=TOPIC, candidate_fields=["id", "amount"])
    handover.publish_mapping(
        name="基础映射", topic=TOPIC,
        field_map={"id": "id", "amount": "amount"},
        compat_report_id=report["report_id"],
        candidate_fields=["id", "amount"],
        confirm=True,
    )
    first = handover.accept_message(topic=TOPIC, message_id="TX-DUP", payload={"id": 9, "amount": 50})
    again = handover.accept_message(topic=TOPIC, message_id="TX-DUP", payload={"id": 9, "amount": 50})
    assert first["accepted"] is True
    assert again["deduplicated"] is True
    summary = handover.income_summary(topic=TOPIC)
    assert summary["count"] == 1
    assert summary["total_amount"] == 50


def test_missing_required_column_on_accepted_mapping_is_rejected():
    """已确认映射下缺少必需列（id）的输入不得计入收入。"""
    reload_store()
    _prepare_schema()
    report = handover.check_compatibility(schema_name=TOPIC, candidate_fields=["id", "amount"])
    handover.publish_mapping(
        name="基础映射", topic=TOPIC,
        field_map={"id": "id", "amount": "amount"},
        compat_report_id=report["report_id"],
        candidate_fields=["id", "amount"],
        confirm=True,
    )
    rejected = handover.accept_message(
        topic=TOPIC, message_id="TX-NO-ID", payload={"amount": 99})
    assert rejected["accepted"] is False
    assert handover.income_summary(topic=TOPIC)["count"] == 0


def test_pending_mapping_accepts_but_not_counted_until_confirmed():
    """没有已确认映射时可按待发布候选受理；确认回执到达前不计入收入汇总。"""
    reload_store()
    _prepare_schema()
    report = handover.check_compatibility(
        schema_name=TOPIC, candidate_fields=["id", "total"])
    pending = handover.publish_mapping(
        name="待确认候选", topic=TOPIC,
        field_map={"id": "id", "amount": "total"},
        compat_report_id=report["report_id"],
        candidate_fields=["id", "total"],
        confirm=False,
    )
    accepted = handover.accept_message(
        topic=TOPIC, message_id="TX-PENDING", payload={"id": 7, "total": 41})
    assert accepted["accepted"] is True
    assert accepted["result"]["mapping_confirmed"] is False
    assert handover.income_summary(topic=TOPIC)["count"] == 0

    handover.confirm_mapping(pending["entry"]["id"])
    summary = handover.income_summary(topic=TOPIC)
    assert summary["count"] == 1
    assert summary["total_amount"] == 41


def test_unknown_compatibility_type_on_both_directions():
    """兼容类型写成无法识别时按双向处理，两个方向都要给出结论。"""
    reload_store()
    _prepare_schema()
    report = handover.check_compatibility(
        schema_name=TOPIC, candidate_fields=["id", "amount", "extra"],
        compatibility_type="兼容的吧")
    assert report["normalized_compatibility_type"] == "bidirectional"
    assert set(report["directions"]) == {"backward", "forward"}
    assert report["compatible"] is True
