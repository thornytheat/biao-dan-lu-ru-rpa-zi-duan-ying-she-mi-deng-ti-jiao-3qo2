"""规则版本：独立草稿、样例预览、原子发布、预览/提交锁版、历史可查。"""
from __future__ import annotations

import json

import pytest

from app import service, store


def fix_in_draft() -> None:
    """把两条冲突规则调高优先级——只写草稿，生产版本不变。"""
    service.update_draft(rule_id="R01", priority=20)
    service.update_draft(rule_id="R03", priority=25)


def test_draft_edit_does_not_touch_production():
    before = store.production_ruleset()
    fix_in_draft()
    after = store.production_ruleset()
    assert after["version"] == before["version"]
    assert after["digest"] == before["digest"]
    assert [r["priority"] for r in after["rules"] if r["id"] == "R01"] == [10]

    head = store.draft_head()
    assert head["revision"] >= 2
    assert {r["id"]: r["priority"] for r in head["rules"]}["R01"] == 20
    assert {d["rule_id"] for d in service.draft_diff()} == {"R01", "R03"}

    # 生产版本仍按原规则跑：冲突仍在
    prod_run = service.dry_run("CG-2026-0001", ruleset_ref=store.production_ruleset()["ref"])
    assert len(prod_run["conflicts"]) == 2

    # 草稿预览：冲突消除
    draft_run = service.dry_run("CG-2026-0001")
    assert draft_run["ruleset_kind"] == "draft"
    assert draft_run["conflicts"] == [] and draft_run["submittable"] is True


def test_publish_switches_whole_ruleset_atomically_and_keeps_history():
    fix_in_draft()
    old = store.production_ruleset()
    published = service.publish(note="解决两处冲突")
    assert published["version"] == old["version"] + 1
    assert published["revision"] == store.draft_head()["revision"] - 1

    # 旧版本原文仍在，且不被覆盖
    kept = store.get_ruleset_version(old["version"])
    assert kept["rules_json"] == old["rules_json"]
    assert {r["id"]: r["priority"] for r in kept["rules"]}["R01"] == 10

    # 发布后草稿从新版本复制，未发布改动清零
    assert store.draft_head()["digest"] == published["digest"]
    assert service.draft_diff() == []


def test_publish_failure_rolls_back_and_keeps_old_version(monkeypatch):
    fix_in_draft()
    before = store.production_ruleset()
    revisions = len(store.list_draft_revisions())

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写入失败")

    monkeypatch.setattr(store, "_insert_draft", boom)
    with pytest.raises(RuntimeError):
        service.publish()

    after = store.production_ruleset()
    assert after["version"] == before["version"]
    assert after["rules_json"] == before["rules_json"]
    assert len(store.list_draft_revisions()) == revisions


def test_invalid_draft_is_rejected_before_publish():
    with pytest.raises(service.RulesetInvalid):
        service.update_draft(rules=[{"id": "R01", "target": "", "priority": 10}])
    with pytest.raises(service.RulesetInvalid):
        service.update_draft(rules=[])


def test_preview_locks_rules_and_rejects_later_version_change():
    fix_in_draft()
    preview = service.dry_run("CG-2026-0001")
    locked_ref = preview["ruleset_ref"]
    assert preview["submittable"] is True

    # 发布草稿后，继续把规则改坏（新草稿里让 amount 变成缺失）
    service.publish()
    broken = [dict(r) for r in store.production_ruleset()["rules"]]
    for rule in broken:
        if rule["target"] == "amount":
            rule["enabled"] = False
    service.update_draft(rules=broken)

    # 旧预览仍然锁定原规则，可提交且字段依据不变
    reloaded = store.get_preview(preview["run_id"])
    assert reloaded["ruleset_ref"] == locked_ref
    assert reloaded["rules_digest"] == preview["rules_digest"]
    assert reloaded["payload"]["amount"] == "1280.50"
    code, body = service.submit("CG-2026-0001", 1, run_id=preview["run_id"])
    assert code == 201, body
    assert body["receipt"]["payload"]["amount"] == "1280.50"
    # 仍记录提交当时锁定的草稿引用；规则快照内容已对应生产版本 v2
    assert body["receipt"]["ruleset_ref"] == locked_ref
    assert body["receipt"]["ruleset_version"] == 2

    # 用锁定后的草稿另建预览则按新规则求值（amount 缺失 → 不可提交）
    fresh = service.dry_run("CG-2026-0001")
    assert fresh["submittable"] is False
    assert fresh["ruleset_ref"] != locked_ref


def test_submit_by_preview_ignores_other_version_and_payload_edits():
    fix_in_draft()
    preview = service.dry_run("CG-2026-0001")
    code, body = service.submit("CG-2026-0001", 2, run_id=preview["run_id"])
    assert code == 409
    assert body["code"] == "preview_version_mismatch"

    # 改源数据后用旧预览提交：仍按预览锁定版本的字段依据落库（payload 不变）
    record = store.get_doc("CG-2026-0001")
    with store.connect() as conn:
        conn.execute(
            "UPDATE source_docs SET payload_json = ? WHERE doc_no = ?",
            (json.dumps({**record["doc"], "amount_total": 9999.0}, ensure_ascii=False), "CG-2026-0001"),
        )
        conn.commit()
    code, body = service.submit("CG-2026-0001", 1, run_id=preview["run_id"])
    assert code == 201, body
    assert body["receipt"]["payload"]["amount"] == "1280.50"


def test_historical_basis_queryable_after_many_publishes():
    fix_in_draft()
    first_preview = service.dry_run("CG-2026-0001")
    service.publish(note="v2")
    assert service.submit("CG-2026-0001", 1, run_id=first_preview["run_id"])[0] == 201

    # 再改草稿并发布 v3（与上一单无关）
    service.update_draft(rule_id="R05", priority=30)
    service.publish(note="v3")
    head_preview = service.dry_run("CG-2026-0001", 2)
    assert service.submit("CG-2026-0001", 2, run_id=head_preview["run_id"])[0] == 201

    stored = store.find_doc_version("CG-2026-0001", 1)
    assert stored["rules_digest"] == first_preview["rules_digest"]
    assert json.loads(stored["rules_json"])[0]["id"] == first_preview["rules_snapshot"][0]["id"]

    data = service.review_data()
    by_receipt = {s["receipt_no"]: s for s in data["submissions"]}
    # ruleset_ref 记录提交时锁定的版本（草稿）；ruleset_version 记录内容对应的生产版本
    assert by_receipt["RCPT-CG-2026-0001-V1"]["ruleset_ref"].startswith("draft-r")
    assert by_receipt["RCPT-CG-2026-0001-V1"]["ruleset_version"] == 2
    # 第二单是在发布 v3 之后按当前（与生产一致的）草稿预览，锁定 v3
    assert by_receipt["RCPT-CG-2026-0001-V2"]["ruleset_ref"] == "v3"
    assert by_receipt["RCPT-CG-2026-0001-V2"]["ruleset_version"] == 3
    counts = {v["ref"]: v["submission_count"] for v in data["versions"]}
    assert counts["v1"] == 0 and counts["v2"] == 1 and counts["v3"] == 1

    # 各版本规则原文历史可查（含最初 v1）
    v1 = store.get_ruleset_version(1)
    assert v1["rules_json"] == store.list_ruleset_versions()[0]["rules_json"]
    assert len(v1["rules"]) == 18


def test_discard_draft_returns_to_production_and_history_kept():
    fix_in_draft()
    service.publish()
    old_versions = [v["version"] for v in store.list_ruleset_versions()]
    service.update_draft(rule_id="R01", priority=99)
    assert service.draft_diff()
    service.update_draft(rules=store.production_ruleset()["rules"], source="reset")
    assert service.draft_diff() == []
    assert [v["version"] for v in store.list_ruleset_versions()] == old_versions


def test_legacy_override_api_writes_draft_only():
    store.set_override("R01", 20, True)
    assert store.production_ruleset()["rules"][0]["priority"] == 10
    assert store.list_overrides()["R01"]["priority"] == 20
    assert service.dry_run("CG-2026-0001")["ruleset_kind"] == "draft"
