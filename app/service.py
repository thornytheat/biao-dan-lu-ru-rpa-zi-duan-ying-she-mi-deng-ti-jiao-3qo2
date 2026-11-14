"""业务编排：规则版本解析、锁版 dry-run 预览、按预览锁定的规则提交。

规则生命周期：
  样例规则 → 生产版本 v1（不可变）→ 每次调整都生成新草稿修订 draft-rN
  → dry-run 预览锁定「生产版本或某个草稿修订」→ 发布把整套草稿原子切换为下一生产版本。

预览与提交都携带完整的规则快照；后续发布不会回写已锁定单据的字段依据与 payload。
"""
from __future__ import annotations

import json
from decimal import Decimal

from . import conflicts as conflicts_mod
from . import contract as contract_mod
from . import idempotency, mapping, settings, store, validation


class DocNotFound(LookupError):
    def __init__(self, doc_no: str):
        super().__init__(f"源单据 {doc_no} 不存在（样例数据只有 3 张单据）")
        self.doc_no = doc_no


class RulesetNotFound(LookupError):
    def __init__(self, ref: str):
        super().__init__(f"规则版本 {ref!r} 不存在（可用生产版本 vN 或草稿修订 draft-rN）")
        self.ref = ref


class PreviewNotFound(LookupError):
    def __init__(self, run_id: str):
        super().__init__(f"预览 {run_id!r} 不存在；请先运行 dry-run，或改为提交当前规则版本")
        self.run_id = run_id


class RulesetInvalid(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _json_safe(obj):
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def load_ruleset() -> dict:
    """样例规则原文（只读参考，不再参与求值）。"""
    with open(settings.RULESET_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# 规则版本
# --------------------------------------------------------------------------

def resolve_ruleset(ref: str | None = None, default: str = "draft") -> dict:
    """按 ref 取规则版本快照；默认取当前草稿（工作台预览用），没有草稿则取生产版本。"""
    if ref:
        text = str(ref).strip()
        if text.startswith("draft-r"):
            try:
                revision = int(text[len("draft-r"):])
            except ValueError:
                raise RulesetNotFound(text)
            draft = store.get_draft_revision(revision)
            if draft is None:
                raise RulesetNotFound(text)
            return _annotate(draft)
        if text.startswith("v") and text[1:].isdigit():
            version = store.get_ruleset_version(int(text[1:]))
            if version is None:
                raise RulesetNotFound(text)
            return _annotate(version)
        raise RulesetNotFound(text)
    if default == "production":
        production = store.production_ruleset()
        if production:
            return _annotate(production)
    draft = store.draft_head()
    production = store.production_ruleset()
    # 草稿与生产版本内容一致时，直接锁定生产版本，避免给同一套规则起两个 ref。
    if draft and production and draft["digest"] == production["digest"]:
        return _annotate(production)
    if draft:
        return _annotate(draft)
    if production:
        return _annotate(production)
    raise RulesetInvalid("没有任何规则版本")


def _annotate(ruleset: dict) -> dict:
    """补上「是否已发布 / 对应生产版本」等展示字段。"""
    item = dict(ruleset)
    item["rules"] = mapping.normalize_rules(item["rules"])
    item["published_version"] = None
    matched = store.latest_version_for_digest(item["digest"])
    if matched:
        item["published_version"] = matched["version"]
        item["published_ref"] = matched["ref"]
    return item


KIND_LABEL = {"production": "生产版本", "draft": "草稿修订"}


def ruleset_brief(ruleset: dict | None) -> dict:
    if not ruleset:
        return {"ref": "", "kind": "", "kind_label": "", "title": "—", "version": None, "revision": None}
    return {
        "ref": ruleset.get("ref", ""),
        "kind": ruleset.get("kind", ""),
        "kind_label": KIND_LABEL.get(ruleset.get("kind", ""), ""),
        "title": ruleset.get("title", ""),
        "version": ruleset.get("version"),
        "revision": ruleset.get("revision"),
        "published_version": ruleset.get("published_version"),
        "diff_count": len(draft_diff()) if ruleset.get("kind") == "draft" else 0,
    }


def lock_summary(run: dict) -> dict:
    """预览/提交锁定到的规则版本摘要（页面与 API 共用）。"""
    by_ref = store.submission_counts_by_ref()
    by_digest = store.preview_counts_by_digest()
    return {
        "ref": run.get("ruleset_ref", ""),
        "kind_label": run.get("ruleset_kind_label", ""),
        "title": run.get("ruleset_title", ""),
        "version": run.get("ruleset_version"),
        "revision": run.get("draft_revision"),
        "digest": run.get("rules_digest", ""),
        "submission_count": by_ref.get(run.get("ruleset_ref", ""), 0),
        "preview_count": by_digest.get(run.get("rules_digest", ""), 0),
        # 已锁定的内容永不随后续发布变化
        "locked": True,
    }


def _inject_identity(targets: list[dict], doc_no: str, version: int) -> None:
    for entry in targets:
        if entry["name"] == "doc_no":
            value, note = doc_no, "单据身份字段：源单据 doc_no，直接注入"
        elif entry["name"] == "version":
            value, note = version, "单据身份字段：本次提交的 version，直接注入"
        else:
            continue
        synthetic = {
            "rule_id": "ID",
            "target": entry["name"],
            "expr": note,
            "priority": 0,
            "base_priority": 0,
            "transform": [],
            "note": note,
            "status": "ok",
            "value": value,
            "value_display": mapping.format_value(value),
            "code": "",
            "reason": "",
        }
        entry.update(
            rules=[synthetic],
            value=value,
            value_display=mapping.format_value(value),
            status="ok",
            conflict=None,
            chosen={"rule_id": "ID", "expr": note, "priority": 0, "overridden": []},
        )


def build_run(doc_no: str, version: int | None = None, ruleset: dict | None = None) -> dict:
    """纯计算：源单据 + 锁定的规则版本 + 契约 → 映射/冲突/校验/幂等键。不写任何表。"""
    record = store.get_doc(doc_no)
    if record is None:
        raise DocNotFound(doc_no)
    doc = record["doc"]
    req_version = int(version if version is not None else doc.get("version", 1))

    ruleset = ruleset or resolve_ruleset()
    rules = mapping.normalize_rules(ruleset["rules"])
    active = mapping.active_rules(rules)
    digest = idempotency.rules_digest(rules)
    contract = contract_mod.load_contract()
    is_draft = ruleset.get("kind") == "draft"
    lock_version = None if is_draft else ruleset.get("version")
    lock_revision = ruleset.get("revision") if is_draft else None

    rule_results = mapping.evaluate_rules(active, doc)
    targets = mapping.resolve_targets(rule_results, contract)
    _inject_identity(targets, doc_no, req_version)

    conflict_list = conflicts_mod.detect(targets)
    check = validation.validate(targets, contract)
    payload = check["payload"]
    psha = idempotency.payload_sha(payload)
    idem_key = idempotency.key(doc_no, req_version, payload)
    lock_ref = ruleset.get("ref", "")

    run = {
        "run_id": idempotency.run_id(doc_no, req_version, f"{lock_ref}:{digest}:{psha}"),
        "rules_digest": digest,
        "ruleset_ref": lock_ref,
        "ruleset_kind": ruleset.get("kind", ""),
        "ruleset_kind_label": KIND_LABEL.get(ruleset.get("kind", ""), ""),
        "ruleset_title": ruleset.get("title", ""),
        "ruleset_version": lock_version,
        "draft_revision": lock_revision,
        "published_version": (ruleset.get("published_version")
                              if ruleset.get("kind") == "draft" else ruleset.get("version")),
        "rules_snapshot": rules,
        "rules_active": active,
        "disabled_rules": [r["id"] for r in rules if not r.get("enabled", True)],
        "doc_no": doc_no,
        "version": req_version,
        "doc_version": int(doc.get("version", 1)),
        "doc": doc,
        "doc_fields": [
            {"name": key, "value_display": mapping.format_value(value)} for key, value in doc.items()
        ],
        "ruleset": ruleset.get("label", "") or load_ruleset().get("ruleset", ""),
        "contract_title": contract.get("form_title", ""),
        "contract_version": contract.get("contract_version", ""),
        "rule_results": rule_results,
        "targets": targets,
        "conflicts": conflict_list,
        "errors": check["errors"],
        "error_groups": validation.group_by_code(check["errors"]),
        "optional_empty": check["optional_empty"],
        "payload": payload,
        "payload_sha": psha,
        "idempotency_key": idem_key,
        "idempotency_key_short": idempotency.short(idem_key, 16),
        "stats": mapping.stats(rule_results),
        "submittable": not conflict_list and not check["errors"],
        "created_at": store.now(),
    }
    run["stats"].update(
        {
            "conflict_count": len(conflict_list),
            "error_count": len(check["errors"]),
            "target_count": len(targets),
            "mapped_count": sum(1 for t in targets if t["status"] == "ok"),
        }
    )
    return _json_safe(run)


def dry_run(
    doc_no: str,
    version: int | None = None,
    persist: bool = True,
    ruleset_ref: str | None = None,
) -> dict:
    """dry-run：不写 submissions / audit_events；只缓存预览快照，并锁定所用规则版本。"""
    ruleset = resolve_ruleset(ruleset_ref)
    run = build_run(doc_no, version, ruleset)
    if persist:
        store.save_preview(run, ruleset)
    return run


def _first_problem(run: dict) -> str:
    if run["conflicts"]:
        return run["conflicts"][0]["summary"]
    if run["errors"]:
        return run["errors"][0]["message"]
    return ""


def _receipt(submission: dict, replayed: bool) -> dict:
    receipt = dict(submission)
    receipt["idem_key_short"] = idempotency.short(submission["idem_key"], 16)
    receipt["replayed"] = replayed
    return receipt


def _locked_from_preview(run_id: str) -> dict:
    """从预览快照取回锁定规则（含完整 rules_json），不依赖当前生产版本。

    保留给需要按锁定规则重新求值的调用方（例如重新预览同一份规则）。
    """
    preview = store.get_preview(run_id, with_rules=True)
    if preview is None:
        raise PreviewNotFound(run_id)
    rules = preview.get("rules_snapshot")
    if rules is None:
        rules = json.loads(_ruleset_version_by_digest(preview.get("rules_digest")) or "[]")
    ref = preview.get("ruleset_ref") or ""
    locked = {
        "ref": ref,
        "kind": "draft" if preview.get("draft_revision") is not None else "production",
        "rules": mapping.normalize_rules(rules),
        "digest": idempotency.rules_digest(mapping.normalize_rules(rules)),
        "version": preview.get("ruleset_version"),
        "revision": preview.get("draft_revision"),
        "title": ref,
        "published_version": preview.get("ruleset_version"),
    }
    return locked


def replay_preview(run_id: str) -> dict:
    """按旧预览锁定的规则快照重新求值（用于“同一个 run 再看一遍”/调试）。"""
    preview = store.get_preview(run_id, with_rules=True)
    if preview is None:
        raise PreviewNotFound(run_id)
    locked = _locked_from_preview(run_id)
    run = build_run(preview["doc_no"], int(preview["version"]), locked)
    run["run_id"] = run_id
    run["locked_from_preview"] = True
    return run


def _ruleset_version_by_digest(digest: str | None) -> str | None:
    if not digest:
        return None
    for version in store.list_ruleset_versions():
        if version["digest"] == digest:
            return version["rules_json"]
    return None


def submit(
    doc_no: str,
    version: int | None = None,
    run_id: str | None = None,
    ruleset_ref: str | None = None,
) -> tuple[int, dict]:
    """提交：返回 (HTTP 状态码, 响应体)。

    201 新建 / 200 replayed / 409 stale_version / 422 校验未通过。
    传 run_id → 严格使用该预览锁定的规则快照与 payload 依据，后续发布改不到它；
    不传 run_id → 锁定当前草稿（没有草稿则生产版本），并保存一份预览供追溯。
    """
    if run_id:
        preview = store.get_preview(run_id, with_rules=True)
        if preview is None:
            raise PreviewNotFound(run_id)
        if doc_no is not None and doc_no != preview["doc_no"]:
            return 409, {
                "code": "preview_doc_mismatch",
                "detail": f"预览 {run_id} 锁定的是单据 {preview['doc_no']}，与请求的 {doc_no} 不一致",
                "preview_doc_no": preview["doc_no"],
            }
        doc_no = preview["doc_no"]
        req_version = int(preview["version"])
        if version is not None and int(version) != req_version:
            return 409, {
                "code": "preview_version_mismatch",
                "detail": f"预览 {run_id} 锁定的是 v{req_version}，与请求的 v{int(version)} 不一致；请按预览版本提交",
                "preview_version": req_version,
            }
        # 锁定的提交内容：直接用预览快照里的 payload / 幂等键 / 规则摘要，
        # 不用当前源数据或当前生产版本重算——源数据或规则之后怎么改都不影响这一单。
        run = {
            **preview,
            "run_id": run_id,
            "locked_from_preview": True,
            "submittable": preview.get("submittable", False),
            "payload": preview.get("payload") or {},
            "payload_sha": preview.get("payload_sha", ""),
            "idempotency_key": preview.get("idempotency_key", ""),
            "rules_digest": preview.get("rules_digest", ""),
            "ruleset_ref": preview.get("ruleset_ref", ""),
            "ruleset_version": preview.get("ruleset_version"),
            "draft_revision": preview.get("draft_revision"),
            "conflicts": preview.get("conflicts") or [],
            "errors": preview.get("errors") or [],
            "rules_snapshot": preview.get("rules_snapshot"),
        }
    else:
        record = store.get_doc(doc_no)
        if record is None:
            raise DocNotFound(doc_no)
        req_version = int(version if version is not None else record["doc"].get("version", 1))
        ruleset = resolve_ruleset(ruleset_ref)
        run = build_run(doc_no, req_version, ruleset)
        store.save_preview(run, ruleset)

    if run["conflicts"] or run["errors"]:
        return 422, {
            "code": "dry_run_blocked",
            "detail": f"映射/校验未通过，未写入任何记录：{_first_problem(run)}",
            "conflicts": run["conflicts"],
            "errors": run["errors"],
            "run_id": run["run_id"],
            "ruleset_ref": run.get("ruleset_ref", ""),
        }

    payload = run["payload"]
    idem_key = run["idempotency_key"]
    sha = run["payload_sha"]

    existing = store.find_by_key(idem_key)
    if existing:
        store.add_audit(
            "replay", doc_no, req_version,
            f"命中既有幂等键 {idempotency.short(idem_key, 16)}，返回回执 {existing['receipt_no']}，不新增记录",
            existing,
        )
        return 200, {
            "replayed": True,
            "detail": f"幂等命中：单据 {doc_no} v{req_version} 与回执 {existing['receipt_no']} 内容一致，未新增记录",
            "receipt": _receipt(existing, True),
            "run_id": run["run_id"],
        }

    latest = store.latest_version(doc_no)
    if latest is not None:
        if req_version < latest:
            store.add_audit(
                "stale_rejected", doc_no, req_version,
                f"版本回退：最新已提交版本 v{latest}，收到 v{req_version}",
            )
            return 409, {
                "code": "stale_version",
                "detail": f"单据 {doc_no} 最新已提交版本为 v{latest}，本次提交 v{req_version} 不是递增版本，已拒绝",
                "latest_version": latest,
            }
        if req_version == latest:
            old = store.find_doc_version(doc_no, req_version) or {}
            store.add_audit(
                "stale_rejected", doc_no, req_version,
                f"同版本 payload 变化但未升版：已存 {old.get('payload_sha', '')[:12]}，本次 {sha[:12]}",
            )
            return 409, {
                "code": "stale_version",
                "detail": (
                    f"单据 {doc_no} 的 v{req_version} 已提交且 payload 内容不同；"
                    "payload 变化时必须显式升版（version+1）后再提交，避免覆盖已入账数据"
                ),
                "latest_version": latest,
                "latest_payload_sha": old.get("payload_sha", ""),
                "payload_sha": sha,
            }

    receipt_no = idempotency.receipt_no(doc_no, req_version)
    # 归属：ruleset_ref 永远记录「提交时锁定的那个版本」；若该快照内容与某个生产版本一致
    # （典型情况：草稿预览 → 之后草稿被发布），额外记录它对应哪个生产版本，便于按版本统计。
    matched = store.latest_version_for_digest(run.get("rules_digest", ""))
    created = store.insert_submission(
        receipt_no=receipt_no,
        idem_key=idem_key,
        doc_no=doc_no,
        version=req_version,
        payload_json=idempotency.canonical_json(payload),
        payload_sha=sha,
        ruleset_ref=run.get("ruleset_ref", ""),
        ruleset_version=matched["version"] if matched else run.get("ruleset_version"),
        draft_revision=run.get("draft_revision"),
        rules_digest=run.get("rules_digest", ""),
        rules_json=json.dumps(run.get("rules_snapshot") or [], ensure_ascii=False),
        run_id=run["run_id"],
    )
    store.add_audit(
        "submit", doc_no, req_version,
        f"新建回执 {receipt_no}，幂等键 {idempotency.short(idem_key, 16)}，"
        f"锁定规则 {run.get('ruleset_ref') or '—'}（快照 {run.get('rules_digest', '')[:12]}）",
        created,
    )
    return 201, {
        "replayed": False,
        "detail": f"提交成功，回执 {receipt_no}（单据 {doc_no} v{req_version}，规则 {run.get('ruleset_ref') or '—'}）",
        "receipt": _receipt(created, False),
        "run_id": run["run_id"],
        "ruleset_ref": run.get("ruleset_ref", ""),
    }


# --------------------------------------------------------------------------
# 规则编辑 / 发布（草稿只增不改，发布整套原子切换）
# --------------------------------------------------------------------------

def _validate_rules(rules: list[dict]) -> list[dict]:
    if not rules:
        raise RulesetInvalid("规则集不能为空")
    seen: set[str] = set()
    cleaned: list[dict] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise RulesetInvalid("规则必须是对象")
        rule_id = str(rule.get("id") or "").strip()
        if not rule_id or rule_id in seen:
            raise RulesetInvalid(f"规则 id 缺失或重复：{rule_id!r}")
        seen.add(rule_id)
        target = str(rule.get("target") or "").strip()
        if not target:
            raise RulesetInvalid(f"规则 {rule_id} 缺少目标字段 target")
        try:
            priority = int(rule.get("priority", 0))
        except (TypeError, ValueError):
            raise RulesetInvalid(f"规则 {rule_id} 的优先级不是整数")
        if priority < 0 or priority > 999:
            raise RulesetInvalid(f"规则 {rule_id} 的优先级需在 0..999 之间")
        cleaned.append(mapping.normalize_rules([rule])[0])
    return cleaned


def update_draft(
    base: dict | None = None,
    rules: list[dict] | None = None,
    rule_id: str | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
    source: str = "edit",
    detail: str = "",
    base_version: int | None = None,
) -> dict:
    """生成新的草稿修订：整份传 rules，或只改一条规则的优先级/启停。"""
    head = base or store.draft_head() or store.production_ruleset()
    if head is None:
        raise RulesetInvalid("没有可编辑的规则版本")
    current = mapping.normalize_rules(rules if rules is not None else head["rules"])
    if rules is None and rule_id is not None:
        found = False
        production = store.production_ruleset() or head
        prod_rules = {r["id"]: r for r in production["rules"]}
        for rule in current:
            if rule["id"] != rule_id:
                continue
            found = True
            if priority is None:
                rule["priority"] = int((prod_rules.get(rule_id) or rule).get("priority", rule["priority"]))
            else:
                rule["priority"] = int(priority)
            if enabled is not None:
                rule["enabled"] = bool(enabled)
        if not found:
            raise RulesetInvalid(f"规则 {rule_id!r} 不存在")
    cleaned = _validate_rules(current)
    production = store.production_ruleset()
    if base_version is None:
        base_version = production["version"] if production else int(head.get("version") or 1)
    return store.create_draft_revision(
        cleaned, int(base_version), source, detail or f"草稿修订（来自 {head['ref']}）"
    )


def publish(revision: int | None = None, note: str = "") -> dict:
    """atomic：整套草稿一次切换为下一个生产版本；失败回滚，旧版本保持不变。"""
    target = store.get_draft_revision(int(revision)) if revision is not None else store.draft_head()
    if target is None:
        raise RulesetInvalid("没有可发布的草稿修订")
    _validate_rules(target["rules"])
    if not target["digest"]:
        raise RulesetInvalid("草稿规则快照摘要为空，拒绝发布")
    label = load_ruleset().get("ruleset", "")
    return store.publish_draft(target["revision"], label, note)


def draft_diff() -> list[dict]:
    """草稿相对生产版本的逐条差异（供页面显示尚未发布的改动）。"""
    production = store.production_ruleset()
    draft = store.draft_head()
    if not production or not draft:
        return []
    base = {r["id"]: r for r in production["rules"]}
    out: list[dict] = []
    for rule in draft["rules"]:
        old = base.get(rule["id"]) or {}
        changes = []
        if int(rule.get("priority", 0)) != int(old.get("priority", 0)):
            changes.append(f"优先级 {old.get('priority', '—')} → {rule.get('priority')}")
        if bool(rule.get("enabled", True)) != bool(old.get("enabled", True)):
            changes.append("启用 → 停用" if not rule.get("enabled", True) else "停用 → 启用")
        if rule.get("expr", "") != old.get("expr", ""):
            changes.append(f"表达式 {old.get('expr', '—')} → {rule.get('expr', '—')}")
        if changes:
            out.append({"rule_id": rule["id"], "target": rule.get("target"),
                        "changes": changes})
    return out


def ruleset_overview() -> dict:
    production = store.production_ruleset()
    draft = store.draft_head()
    versions = store.list_ruleset_versions()
    drafts = store.list_draft_revisions()
    by_digest = store.submission_counts_by_digest()
    by_ref = store.submission_counts_by_ref()
    preview_by_digest = store.preview_counts_by_digest()

    version_rows = []
    for version in versions:
        version_rows.append({
            **version,
            # 生产版本按「规则快照内容」统计：先按草稿锁版、发布后内容等同该版本的单据也算进来
            "submission_count": by_digest.get(version["digest"], 0),
            "locked_ref_count": by_ref.get(version["ref"], 0),
            "preview_count": preview_by_digest.get(version["digest"], 0),
            "is_production": bool(production and version["version"] == production["version"]),
        })
    draft_rows = []
    head_revision = draft["revision"] if draft else None
    for draft in drafts:
        draft_rows.append({
            **draft,
            "submission_count": by_ref.get(draft["ref"], 0),
            "preview_count": preview_by_digest.get(draft["digest"], 0),
            "is_head": draft["revision"] == head_revision,
        })
    production_info = None
    if production:
        production_info = {
            **production,
            "submission_count": by_digest.get(production["digest"], 0),
            "locked_ref_count": by_ref.get(production["ref"], 0),
            "preview_count": preview_by_digest.get(production["digest"], 0),
        }
    draft_info = None
    if draft:
        draft_info = {
            **draft,
            "submission_count": by_ref.get(draft["ref"], 0),
            "preview_count": preview_by_digest.get(draft["digest"], 0),
            "diff": draft_diff(),
        }
    return {
        "production": production_info,
        "draft": draft_info,
        "versions": version_rows,
        "drafts": draft_rows,
        "events": store.list_ruleset_events(40),
    }


# --------------------------------------------------------------------------
# 页面辅助
# --------------------------------------------------------------------------

def rules_table(run: dict | None = None, ruleset: dict | None = None) -> list[dict]:
    """规则表：锁定快照里的优先级/启停 + 相对生产版本的改动 + 本次求值命中情况。"""
    snapshot = (ruleset or {}).get("rules") or (run or {}).get("rules_snapshot") or []
    if not snapshot:
        snapshot = (resolve_ruleset() or {}).get("rules", [])
    production = store.production_ruleset()
    prod_rules = {r["id"]: r for r in (production or {}).get("rules", [])}
    hits = {r["rule_id"]: r for r in (run or {}).get("rule_results", [])}
    rows: list[dict] = []
    for rule in mapping.normalize_rules(snapshot):
        prod = prod_rules.get(rule["id"], {})
        prod_priority = int(prod.get("priority", rule.get("base_priority", rule["priority"])))
        prod_enabled = bool(prod.get("enabled", True))
        item = {
            "id": rule["id"],
            "target": rule["target"],
            "expr": rule.get("expr", ""),
            "transform": list(rule.get("transform") or []),
            "note": rule.get("note", ""),
            "base_priority": prod_priority,
            "enabled": bool(rule.get("enabled", True)),
            "production_enabled": prod_enabled,
            "overridden": int(rule["priority"]) != prod_priority or bool(rule.get("enabled", True)) != prod_enabled,
            "hit": hits.get(rule["id"]),
        }
        item["priority"] = int(rule["priority"])
        rows.append(item)
    return rows


def review_data(receipt_no: str | None = None) -> dict:
    submissions = store.list_submissions()
    replays = store.replay_counts()
    by_ref = store.submission_counts_by_ref()
    for item in submissions:
        item["idem_key_short"] = idempotency.short(item["idem_key"], 16)
        item["payload_sha_short"] = idempotency.short(item["payload_sha"], 12)
        item["replayed_times"] = replays.get(item["receipt_no"], 0)
        item["rules_snapshot"] = json.loads(item["rules_json"]) if item.get("rules_json") else None
        item["rules_digest_short"] = idempotency.short(item.get("rules_digest") or "", 12)
    chains: dict[str, dict] = {}
    for item in submissions:
        chain = chains.setdefault(
            item["doc_no"],
            {"doc_no": item["doc_no"], "versions": [], "latest_version": item["version"]},
        )
        chain["versions"].append(item)
        chain["latest_version"] = max(chain["latest_version"], item["version"])
    for chain in chains.values():
        chain["versions"].sort(key=lambda v: v["version"])
        for item in chain["versions"]:
            item["is_latest"] = item["version"] == chain["latest_version"]
    receipt = store.get_submission(receipt_no) if receipt_no else (submissions[-1] if submissions else None)
    if receipt:
        order = [f["name"] for f in contract_mod.load_contract()["fields"]]
        receipt = dict(receipt)
        receipt["payload"] = {k: receipt["payload"][k] for k in order if k in receipt["payload"]}
        receipt["idem_key_short"] = idempotency.short(receipt["idem_key"], 16)
        receipt["replayed_times"] = replays.get(receipt["receipt_no"], 0)
        receipt["rules_snapshot"] = json.loads(receipt["rules_json"]) if receipt.get("rules_json") else None
        receipt["rules_digest_short"] = idempotency.short(receipt.get("rules_digest") or "", 12)
    versions = store.list_ruleset_versions()
    version_by_digest = {v["digest"]: v for v in versions}
    by_digest = store.submission_counts_by_digest()
    for item in submissions:
        matched = version_by_digest.get(item.get("rules_digest") or "")
        item["published_version"] = matched["version"] if matched else None
    version_rows = [
        {
            **v,
            # 按规则快照内容统计：草稿锁版、发布后内容等同该版本的单据也计入该版本
            "submission_count": by_digest.get(v["digest"], 0),
            "locked_ref_count": by_ref.get(v["ref"], 0),
        }
        for v in versions
    ]
    return {
        "submissions": submissions,
        "chains": sorted(chains.values(), key=lambda c: c["doc_no"]),
        "audits": store.list_audits(40),
        "ruleset_events": store.list_ruleset_events(20),
        "versions": version_rows,
        "submission_counts_by_ref": by_ref,
        "submission_counts_by_digest": by_digest,
        "receipt": receipt,
        "submission_count": len(submissions),
        "override_count": len(store.list_overrides()),
    }
