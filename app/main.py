"""FastAPI 路由（薄）：只做参数解析、调用 service、渲染模板。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import contract as contract_mod
from . import service, settings, store

app = FastAPI(title=settings.APP_NAME)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["pretty_json"] = lambda value: json.dumps(
    value, ensure_ascii=False, indent=2, sort_keys=True
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

SEED_INFO = store.init_db()


def _base(request: Request, **extra) -> dict:
    context = {
        "request": request,
        "app_name": settings.APP_NAME,
        "seed_info": SEED_INFO,
        "submission_count": store.count_submissions(),
        "override_count": len(store.list_overrides()),
    }
    context.update(extra)
    return context


def _workbench_context(
    doc_no: str | None,
    version: int | None,
    run_flag: bool,
    ruleset_ref: str | None = None,
) -> dict:
    docs = store.list_docs()
    names = [d["doc_no"] for d in docs]
    selected = doc_no if doc_no in names else (names[0] if names else None)
    # 工作台预览默认锁定草稿；显式 ref 可以预览指定生产版本。
    ruleset = service.resolve_ruleset(ruleset_ref)
    run = None
    if selected:
        run = service.build_run(selected, version, ruleset)
        if run_flag:
            store.save_preview(run, ruleset)
    contract = contract_mod.load_contract()
    production = store.production_ruleset()
    draft = store.draft_head()
    overview = service.ruleset_overview()
    return {
        "docs": docs,
        "selected": selected,
        "version": version,
        "run": run,
        "show_run": run_flag and run is not None,
        "ruleset": ruleset,
        "production": production,
        "draft": draft,
        "draft_diff": service.draft_diff(),
        "overview": overview,
        "locked": service.lock_summary(run) if run else None,
        "rules": service.rules_table(run, ruleset),
        "contract": contract,
        "fields": contract["fields"],
        "overrides": store.list_overrides(),
        "suggestions": [c["fix"] for c in (run["conflicts"] if run else [])],
    }


@app.get("/health")
def health():
    production = store.production_ruleset()
    return JSONResponse(
        {
            "status": "ok",
            "app": settings.APP_SHORT,
            "docs": store.count_docs(),
            "submissions": store.count_submissions(),
            "seed_version": SEED_INFO["seed_version"],
            "ruleset_version": production["version"] if production else None,
            "draft_revision": (store.draft_head() or {}).get("revision"),
        }
    )


@app.get("/")
def index(request: Request):
    contract = contract_mod.load_contract()
    rules = service.rules_table()
    docs = store.list_docs()
    recent_submissions = sorted(
        store.list_submissions(), key=lambda item: item.get("created_at") or "", reverse=True
    )[:6]
    overview = service.ruleset_overview()
    return templates.TemplateResponse(
        request,
        "index.html",
        _base(
            request,
            page="index",
            recent_submissions=recent_submissions,
            docs=docs,
            contract=contract,
            fields=contract["fields"],
            rules=rules,
            ruleset=service.load_ruleset().get("ruleset", ""),
            overview=overview,
            conflict_groups=[
                {"target": "amount", "rules": ["R01", "R02"], "priority": 10, "values": ["1280.50", "1446.97"]},
                {"target": "expense_type", "rules": ["R03", "R04"], "priority": 15, "values": ["travel", "travel_fee"]},
            ],
        ),
    )


@app.get("/workbench")
def workbench(
    request: Request,
    doc: str | None = None,
    version: int | None = None,
    run: int = 0,
    rules: str | None = None,
):
    try:
        context = _workbench_context(doc, version, bool(run), rules)
        error = None
    except (service.RulesetNotFound, service.RulesetInvalid) as exc:
        context = _workbench_context(doc, version, False)
        error = str(exc)
    return templates.TemplateResponse(
        request, "workbench.html", _base(request, page="workbench", **context, error=error)
    )


@app.post("/workbench/rules")
async def workbench_rules(request: Request):
    form = await request.form()
    doc = form.get("doc") or None
    version = form.get("version")
    rules_ref = form.get("rules_ref") or None
    base = service.resolve_ruleset(rules_ref)
    rules = [dict(rule) for rule in base["rules"]]
    changed = 0
    for rule in rules:
        rule_id = rule["id"]
        raw_priority = form.get(f"priority_{rule_id}")
        enabled = f"enabled_{rule_id}" in form
        if raw_priority is None:
            continue
        try:
            priority = int(str(raw_priority).strip())
        except ValueError:
            continue
        priority = max(0, min(999, priority))
        if priority != int(rule.get("priority", 0)) or enabled != bool(rule.get("enabled", True)):
            rule["priority"] = priority
            rule["enabled"] = enabled
            changed += 1
    draft = None
    if changed:
        draft = service.update_draft(
            rules=rules, source="workbench",
            detail=f"工作台保存 {changed} 条改动",
            base_version=int(base.get("version") or 1),
        )
    target_version = int(version) if version else None
    url = f"/workbench?doc={doc or ''}&run=1"
    if draft:
        url += f"&rules={draft['ref']}"
    if target_version:
        url += f"&version={target_version}"
    return RedirectResponse(url=url, status_code=303)


@app.post("/workbench/submit")
def workbench_submit(
    request: Request,
    doc_no: str = Form(...),
    version: int | None = Form(None),
    run_id: str | None = Form(None),
):
    try:
        status_code, body = service.submit(doc_no, version, run_id=run_id or None)
    except service.DocNotFound as exc:
        context = _workbench_context(None, None, False)
        return templates.TemplateResponse(
            request,
            "workbench.html",
            _base(request, page="workbench", **context, error=str(exc)),
            status_code=404,
        )
    except service.PreviewNotFound as exc:
        context = _workbench_context(doc_no, version, False)
        return templates.TemplateResponse(
            request,
            "workbench.html",
            _base(request, page="workbench", **context, error=str(exc)),
            status_code=404,
        )
    if status_code in (200, 201):
        receipt_no = body["receipt"]["receipt_no"]
        suffix = "&replayed=1" if body.get("replayed") else ""
        return RedirectResponse(url=f"/review?receipt={receipt_no}{suffix}", status_code=303)
    context = _workbench_context(doc_no, version, True)
    notice = f"提交被拒绝（HTTP {status_code}）：{body.get('detail', '')}"
    return templates.TemplateResponse(
        request,
        "workbench.html",
        _base(request, page="workbench", **context, error=notice),
        status_code=status_code,
    )


@app.post("/workbench/reset")
def workbench_reset(request: Request, doc: str = Form("")):
    service.update_draft(source="reset", detail="丢弃草稿改动，恢复为生产版本")
    return RedirectResponse(url=f"/workbench?doc={doc}&run=1", status_code=303)


@app.post("/workbench/discard")
def workbench_discard(request: Request, doc: str = Form("")):
    service.update_draft(source="reset", detail="丢弃草稿改动，恢复为生产版本")
    return RedirectResponse(url=f"/rules?doc={doc}", status_code=303)


@app.post("/workbench/publish")
def workbench_publish(request: Request, doc: str = Form(""), note: str = Form("")):
    try:
        published = service.publish(note=note.strip())
    except (service.RulesetInvalid, KeyError) as exc:
        context = _workbench_context(doc, None, False)
        return templates.TemplateResponse(
            request,
            "workbench.html",
            _base(request, page="workbench", **context, error=f"发布失败，生产版本保持不变：{exc}"),
            status_code=409,
        )
    return RedirectResponse(url=f"/workbench?doc={doc}&run=1&rules={published['ref']}", status_code=303)


def _lock_check(run: dict) -> dict | None:
    """校验预览快照的完整性：存的规则原文摘要应与记录的 rules_digest 一致。"""
    rules = run.get("rules_snapshot")
    if not rules:
        return None
    from . import idempotency

    recomputed = idempotency.rules_digest(rules)
    return {
        "ok": recomputed == run.get("rules_digest"),
        "recomputed": recomputed,
        "stored": run.get("rules_digest", ""),
    }


@app.get("/result/{run_id}")
def result(request: Request, run_id: str):
    run = store.get_preview(run_id)
    if run is None:
        return templates.TemplateResponse(
            request,
            "missing.html",
            _base(
                request,
                page="missing",
                error=f"预览 {run_id} 不存在（预览按单据+版本+规则快照缓存，规则改动后 run_id 会变化）",
                back_url="/workbench",
            ),
            status_code=404,
        )
    # 回看始终展示锁定的快照内容；再以锁定规则重算一遍可校验快照是否一致。
    run["lock_check"] = _lock_check(run)
    return templates.TemplateResponse(
        request,
        "result.html",
        _base(request, page="result", run=run, contract=contract_mod.load_contract()),
    )


@app.get("/review")
def review(request: Request, receipt: str | None = None, replayed: int = 0):
    data = service.review_data(receipt)
    notice = "本次提交命中幂等键，返回既有回执，未新增记录（replayed=true）" if replayed else None
    contract = contract_mod.load_contract()
    return templates.TemplateResponse(
        request,
        "review.html",
        _base(request, page="review", **data, contract=contract, notice=notice, submitted=receipt or None),
    )


@app.get("/rules")
def rules_page(request: Request, doc: str | None = None, version: str | None = None):
    overview = service.ruleset_overview()
    docs = store.list_docs()
    selected = doc if doc in [d["doc_no"] for d in docs] else (docs[0]["doc_no"] if docs else None)
    preview_ref = version or ((overview.get("draft") or {}).get("ref") if overview.get("draft") else None)
    try:
        current = service.resolve_ruleset(preview_ref)
    except (service.RulesetNotFound, service.RulesetInvalid):
        current = service.resolve_ruleset()
    return templates.TemplateResponse(
        request,
        "rules.html",
        _base(
            request,
            page="rules",
            overview=overview,
            selected_doc=selected,
            current=current,
            draft_rules=service.rules_table(ruleset=current),
            production=overview.get("production"),
            draft=overview.get("draft"),
        ),
    )


@app.get("/api/rules")
def api_rules():
    run = None
    docs = store.list_docs()
    ruleset = service.resolve_ruleset()
    if docs:
        run = service.build_run(docs[0]["doc_no"], None, ruleset)
    overview = service.ruleset_overview()
    return {
        "ruleset": service.load_ruleset().get("ruleset", ""),
        "production_version": (overview.get("production") or {}).get("version"),
        "draft_revision": (overview.get("draft") or {}).get("revision"),
        "draft_ref": ruleset.get("ref"),
        "draft_diff": service.draft_diff(),
        "overrides": store.list_overrides(),
        "rules": service.rules_table(run, ruleset),
    }


@app.get("/api/ruleset")
def api_ruleset():
    overview = service.ruleset_overview()
    return {
        "production": overview.get("production"),
        "draft": overview.get("draft"),
        "versions": [
            {k: v[k] for k in ("version", "ref", "digest", "submission_count", "preview_count",
                              "published_at", "note")}
            for v in overview.get("versions", [])
        ],
        "events": overview.get("events", []),
    }


@app.post("/api/rules/priority")
async def api_rules_priority(payload: dict):
    rule_id = payload.get("rule_id")
    known = {r["id"] for r in service.resolve_ruleset()["rules"]}
    if not rule_id or rule_id not in known:
        return JSONResponse(
            {"detail": f"规则 {rule_id!r} 不存在，可用规则：{', '.join(sorted(known))}"}, status_code=404
        )
    priority = payload.get("priority")
    enabled = payload.get("enabled", True)
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            return JSONResponse({"detail": "priority 必须是整数"}, status_code=422)
        if priority < 0 or priority > 999:
            return JSONResponse({"detail": "priority 需在 0..999 之间"}, status_code=422)
    if not isinstance(enabled, bool):
        return JSONResponse({"detail": "enabled 必须是布尔值"}, status_code=422)
    try:
        draft = service.update_draft(rule_id=rule_id, priority=priority, enabled=enabled,
                                     source="api", detail=f"调整 {rule_id} 优先级/启停")
    except service.RulesetInvalid as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    return {
        "rule_id": rule_id,
        "priority": priority,
        "enabled": enabled,
        "draft_revision": draft["revision"],
        "draft_ref": draft["ref"],
        "overrides": store.list_overrides(),
        "detail": f"已写入草稿 {draft['ref']}（生产版本未变，发布后才生效）",
    }


@app.post("/api/rules/reset")
def api_rules_reset():
    production = store.production_ruleset()
    if production is None:
        return {"removed": 0, "detail": "没有生产版本可恢复"}
    removed = len(service.draft_diff()) if (store.draft_head() or {}) else 0
    draft = service.update_draft(
        rules=production["rules"], source="reset", detail="丢弃草稿改动，恢复为生产版本"
    )
    return {
        "removed": removed,
        "draft_ref": draft["ref"],
        "detail": f"已丢弃 {removed} 条草稿改动，草稿回到生产版本 v{production['version']}",
    }


@app.post("/api/ruleset/publish")
async def api_ruleset_publish(payload: dict | None = None):
    payload = payload or {}
    revision = payload.get("revision")
    if revision is not None:
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            return JSONResponse({"detail": "revision 必须是整数"}, status_code=422)
    try:
        published = service.publish(revision=revision, note=str(payload.get("note") or ""))
    except (service.RulesetInvalid, KeyError) as exc:
        return JSONResponse(
            {"code": "publish_failed", "detail": f"发布失败，生产版本保持不变：{exc}"}, status_code=409
        )
    return {
        "version": published["version"],
        "ref": published["ref"],
        "digest": published["digest"],
        "revision": published["revision"],
        "published_at": published["published_at"],
        "detail": f"已整套发布生产版本 v{published['version']}（来自草稿 r{published['revision']}）",
    }


@app.post("/api/ruleset/discard")
def api_ruleset_discard():
    production = store.production_ruleset()
    if production is None:
        return JSONResponse({"detail": "没有生产版本可恢复"}, status_code=409)
    draft = service.update_draft(
        rules=production["rules"], source="reset", detail="丢弃草稿改动，恢复为生产版本"
    )
    return {"draft_ref": draft["ref"], "detail": f"草稿已回到生产版本 v{production['version']}"}


@app.post("/api/dry-run")
async def api_dry_run(payload: dict | None = None):
    payload = payload or {}
    doc_no = payload.get("doc_no")
    version = payload.get("version")
    ruleset_ref = payload.get("ruleset_ref") or payload.get("rules") or None
    if not doc_no:
        return JSONResponse({"detail": "缺少 doc_no 参数"}, status_code=400)
    try:
        version = int(version) if version is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"detail": "version 必须是整数"}, status_code=422)
    if version is not None and version < 1:
        return JSONResponse({"detail": "version 必须大于等于 1"}, status_code=422)
    try:
        run = service.dry_run(doc_no, version, ruleset_ref=ruleset_ref)
    except service.DocNotFound as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    except (service.RulesetNotFound, service.RulesetInvalid) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {
        "run_id": run["run_id"],
        "doc_no": run["doc_no"],
        "version": run["version"],
        "submittable": run["submittable"],
        "conflict_count": len(run["conflicts"]),
        "error_count": len(run["errors"]),
        "idempotency_key": run["idempotency_key"],
        "payload": run["payload"],
        "conflicts": run["conflicts"],
        "errors": run["errors"],
        "ruleset_ref": run["ruleset_ref"],
        "ruleset_kind": run["ruleset_kind"],
        "ruleset_version": run["ruleset_version"],
        "draft_revision": run["draft_revision"],
        "rules_digest": run["rules_digest"],
        "result_url": f"/result/{run['run_id']}",
    }


@app.post("/api/submit")
async def api_submit(payload: dict | None = None):
    payload = payload or {}
    doc_no = payload.get("doc_no")
    version = payload.get("version")
    run_id = payload.get("run_id")
    ruleset_ref = payload.get("ruleset_ref") or None
    if not doc_no and not run_id:
        return JSONResponse({"detail": "缺少 doc_no 参数"}, status_code=400)
    try:
        version = int(version) if version is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"detail": "version 必须是整数"}, status_code=422)
    try:
        status_code, body = service.submit(doc_no, version, run_id=run_id, ruleset_ref=ruleset_ref)
    except service.DocNotFound as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    except service.PreviewNotFound as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    except (service.RulesetNotFound, service.RulesetInvalid) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return JSONResponse(body, status_code=status_code)


@app.get("/api/previews")
def api_previews():
    return {"previews": store.list_previews(40)}


@app.get("/api/submissions")
def api_submissions():
    data = service.review_data()
    return {
        "count": data["submission_count"],
        "submissions": [
            {k: s[k] for k in ("receipt_no", "doc_no", "version", "payload", "payload_sha",
                              "ruleset_ref", "ruleset_version", "draft_revision", "rules_digest",
                              "run_id", "created_at")}
            for s in data["submissions"]
        ],
    }
