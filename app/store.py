"""SQLite 访问：源单据、不可变规则版本、独立草稿、预览锁定、提交与回执、审计流水。

规则不再「就地覆盖」：生产规则落在 ruleset_versions（只增不改），
所有调整先进入 rule_draft_revisions（每次编辑都是新修订），发布时整套切换。
预览与提交都会记录锁定到的规则快照（ref + digest + 完整 rules_json），
后续再发布也改不到已锁定单据的字段依据与提交内容。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import settings

DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_docs (
  doc_no TEXT PRIMARY KEY,
  version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  seeded_at TEXT NOT NULL
);
-- legacy：旧版本的优先级覆盖表，只在首次建生产版本时迁移一次，之后只读忽略
CREATE TABLE IF NOT EXISTS rule_overrides (
  rule_id TEXT PRIMARY KEY,
  priority INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ruleset_versions (
  version INTEGER PRIMARY KEY,
  label TEXT NOT NULL DEFAULT '',
  rules_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  revision INTEGER,
  note TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_draft_revisions (
  revision INTEGER PRIMARY KEY,
  base_version INTEGER NOT NULL,
  rules_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'edit',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ruleset_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  ref TEXT NOT NULL DEFAULT '',
  version INTEGER,
  revision INTEGER,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS previews (
  run_id TEXT PRIMARY KEY,
  doc_no TEXT NOT NULL,
  version INTEGER NOT NULL,
  rules_digest TEXT NOT NULL,
  ruleset_ref TEXT NOT NULL DEFAULT '',
  ruleset_version INTEGER,
  draft_revision INTEGER,
  rules_json TEXT,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_no TEXT NOT NULL UNIQUE,
  idem_key TEXT NOT NULL UNIQUE,
  doc_no TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha TEXT NOT NULL,
  ruleset_ref TEXT NOT NULL DEFAULT '',
  ruleset_version INTEGER,
  draft_revision INTEGER,
  rules_digest TEXT NOT NULL DEFAULT '',
  rules_json TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (doc_no, version)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id INTEGER,
  receipt_no TEXT,
  doc_no TEXT NOT NULL,
  version INTEGER NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""

# 旧库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里按需 ALTER。
PREVIEW_COLUMNS = {
    "ruleset_ref": "TEXT NOT NULL DEFAULT ''",
    "ruleset_version": "INTEGER",
    "draft_revision": "INTEGER",
    "rules_json": "TEXT",
}
SUBMISSION_COLUMNS = {
    "ruleset_ref": "TEXT NOT NULL DEFAULT ''",
    "ruleset_version": "INTEGER",
    "draft_revision": "INTEGER",
    "rules_digest": "TEXT NOT NULL DEFAULT ''",
    "rules_json": "TEXT NOT NULL DEFAULT ''",
    "run_id": "TEXT NOT NULL DEFAULT ''",
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db() -> dict:
    with connect() as conn:
        conn.executescript(DDL)
        _ensure_columns(conn, "previews", PREVIEW_COLUMNS)
        _ensure_columns(conn, "submissions", SUBMISSION_COLUMNS)
        conn.commit()
    info = seed_docs()
    ensure_initial_ruleset()
    return info


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def seed_docs() -> dict:
    """幂等 seed：重复启动 / 重复调用都不会重复插入单据。"""
    with open(settings.SOURCE_DOCS_FILE, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    docs = payload["docs"]
    inserted, skipped = 0, 0
    with connect() as conn:
        conn.executescript(DDL)
        already = meta_get(conn, "docs_seed_version")
        if already == settings.SEED_VERSION:
            skipped = len(docs)
            return {"seed_version": already, "inserted": 0, "skipped": skipped,
                    "total": conn.execute("SELECT COUNT(*) AS c FROM source_docs").fetchone()["c"]}
        for doc in docs:
            exists = conn.execute("SELECT 1 FROM source_docs WHERE doc_no = ?", (doc["doc_no"],)).fetchone()
            if exists:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO source_docs (doc_no, version, payload_json, seeded_at) VALUES (?,?,?,?)",
                (doc["doc_no"], int(doc.get("version", 1)), json.dumps(doc, ensure_ascii=False), now()),
            )
            inserted += 1
        meta_set(conn, "docs_seed_version", settings.SEED_VERSION)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) AS c FROM source_docs").fetchone()["c"]
    return {"seed_version": settings.SEED_VERSION, "inserted": inserted, "skipped": skipped, "total": total}


# --------------------------------------------------------------------------
# 规则版本（只增不改）
# --------------------------------------------------------------------------

def _sample_rules() -> list[dict]:
    with open(settings.RULESET_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)["rules"]


def _normalize(rules: list[dict]) -> list[dict]:
    from .mapping import normalize_rules

    return normalize_rules(rules)


def _digest(rules: list[dict]) -> str:
    from .idempotency import rules_digest

    return rules_digest(rules)


def ensure_initial_ruleset() -> None:
    """没有生产版本时，用样例规则 + 旧覆盖建 v1；同时生成首个草稿修订。"""
    with connect() as conn:
        exists = conn.execute("SELECT COUNT(*) AS c FROM ruleset_versions").fetchone()["c"]
    if exists:
        with connect() as conn:
            draft = conn.execute("SELECT COUNT(*) AS c FROM rule_draft_revisions").fetchone()["c"]
            if not draft:
                prod = _production_row(conn)
                if prod:
                    _insert_draft(conn, json.loads(prod["rules_json"]), prod["version"], "seed")
                    _event(conn, "draft_seed", f"draft-r1", prod["version"], 1, "从生产版本复制草稿")
                    conn.commit()
        return

    rules = _normalize(_sample_rules())
    with connect() as conn:
        overrides = {
            r["rule_id"]: {"priority": r["priority"], "enabled": bool(r["enabled"])}
            for r in conn.execute("SELECT * FROM rule_overrides").fetchall()
        }
    for rule in rules:
        override = overrides.get(rule["id"]) or {}
        if override.get("priority") is not None:
            rule["priority"] = int(override["priority"])
        if override.get("enabled") is False:
            rule["enabled"] = False
    digest = _digest(rules)
    with connect() as conn:
        conn.execute(
            "INSERT INTO ruleset_versions (version, label, rules_json, digest, revision, note, published_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (1, _ruleset_label(), json.dumps(rules, ensure_ascii=False), digest, None, "初始化：样例规则", now()),
        )
        _event(conn, "publish", "v1", 1, None, "初始化生产版本 v1")
        _insert_draft(conn, rules, 1, "seed")
        conn.commit()


def _ruleset_label() -> str:
    try:
        with open(settings.RULESET_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh).get("ruleset", "")
    except (OSError, ValueError):
        return ""


def _event(
    conn: sqlite3.Connection,
    action: str,
    ref: str = "",
    version: int | None = None,
    revision: int | None = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO ruleset_events (action, ref, version, revision, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (action, ref, version, revision, detail, now()),
    )


def _production_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ruleset_versions ORDER BY version DESC LIMIT 1").fetchone()


def _version_dict(row: sqlite3.Row) -> dict:
    return {
        "version": row["version"],
        "label": row["label"],
        "rules": json.loads(row["rules_json"]),
        "rules_json": row["rules_json"],
        "digest": row["digest"],
        "revision": row["revision"],
        "note": row["note"],
        "published_at": row["published_at"],
        "ref": f"v{row['version']}",
        "kind": "production",
        "title": f"生产 v{row['version']}",
    }


def _draft_dict(row: sqlite3.Row) -> dict:
    return {
        "revision": row["revision"],
        "base_version": row["base_version"],
        "rules": json.loads(row["rules_json"]),
        "rules_json": row["rules_json"],
        "digest": row["digest"],
        "source": row["source"],
        "created_at": row["created_at"],
        "ref": f"draft-r{row['revision']}",
        "kind": "draft",
        "version": row["base_version"],
        "title": f"草稿 r{row['revision']}（基于 v{row['base_version']}）",
    }


def list_ruleset_versions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM ruleset_versions ORDER BY version").fetchall()
    return [_version_dict(r) for r in rows]


def get_ruleset_version(version: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM ruleset_versions WHERE version = ?", (int(version),)).fetchone()
    return _version_dict(row) if row else None


def production_version() -> int | None:
    with connect() as conn:
        row = conn.execute("SELECT MAX(version) AS v FROM ruleset_versions").fetchone()
    return row["v"] if row and row["v"] is not None else None


def production_ruleset() -> dict | None:
    with connect() as conn:
        row = _production_row(conn)
    return _version_dict(row) if row else None


def latest_version_for_digest(digest: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM ruleset_versions WHERE digest = ? ORDER BY version DESC LIMIT 1", (digest,)
        ).fetchone()
    return _version_dict(row) if row else None


def list_draft_revisions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM rule_draft_revisions ORDER BY revision").fetchall()
    return [_draft_dict(r) for r in rows]


def get_draft_revision(revision: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM rule_draft_revisions WHERE revision = ?", (int(revision),)
        ).fetchone()
    return _draft_dict(row) if row else None


def draft_head() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM rule_draft_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    return _draft_dict(row) if row else None


def _insert_draft(
    conn: sqlite3.Connection, rules: list[dict], base_version: int, source: str
) -> dict:
    rules = _normalize(rules)
    digest = _digest(rules)
    row = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 AS r FROM rule_draft_revisions").fetchone()
    revision = int(row["r"])
    conn.execute(
        "INSERT INTO rule_draft_revisions (revision, base_version, rules_json, digest, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (revision, int(base_version), json.dumps(rules, ensure_ascii=False), digest, source, now()),
    )
    return {"revision": revision, "base_version": int(base_version), "rules": rules,
            "digest": digest, "source": source, "ref": f"draft-r{revision}"}


def create_draft_revision(rules: list[dict], base_version: int, source: str = "edit",
                          detail: str = "") -> dict:
    with connect() as conn:
        created = _insert_draft(conn, rules, base_version, source)
        _event(conn, "draft_edit", created["ref"], int(base_version), created["revision"],
               detail or "草稿修订")
        conn.commit()
    return get_draft_revision(created["revision"]) or created


def publish_draft(revision: int, label: str, note: str = "") -> dict:
    """把某个草稿修订整套发布为下一个生产版本。

    单个事务内完成：新增版本 + 事件 + 从新版本复制下一份草稿。
    任何一步失败都回滚，生产版本保持原样（不会留下半套规则）。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM rule_draft_revisions WHERE revision = ?", (int(revision),)
        ).fetchone()
        if row is None:
            raise KeyError(f"草稿修订 r{revision} 不存在")
        rules_json = row["rules_json"]
        digest = row["digest"]
        row2 = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM ruleset_versions").fetchone()
        version = int(row2["v"])
        conn.execute(
            "INSERT INTO ruleset_versions (version, label, rules_json, digest, revision, note, published_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (version, label, rules_json, digest, int(revision), note, now()),
        )
        _event(conn, "publish", f"v{version}", version, int(revision),
               f"发布草稿 r{revision}" + (f"：{note}" if note else ""))
        _insert_draft(conn, json.loads(rules_json), version, "publish")
        conn.commit()
    return get_ruleset_version(version) or {}


def list_ruleset_events(limit: int = 40) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ruleset_events ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# 兼容层：旧代码里的「规则覆盖」现在等价于「草稿相对生产版本的差异」
# --------------------------------------------------------------------------

def _rule_diff(base_rules: list[dict], draft_rules: list[dict]) -> dict[str, dict]:
    base = {r["id"]: r for r in base_rules}
    out: dict[str, dict] = {}
    for rule in draft_rules:
        old = base.get(rule["id"])
        if old is None:
            continue
        if int(rule.get("priority", 0)) != int(old.get("priority", 0)) or \
                bool(rule.get("enabled", True)) != bool(old.get("enabled", True)):
            out[rule["id"]] = {
                "priority": int(rule.get("priority", 0)),
                "enabled": bool(rule.get("enabled", True)),
            }
    return out


def list_overrides() -> dict[str, dict]:
    """草稿相对生产版本的差异（供页面显示「未发布的改动」）。"""
    production = production_ruleset()
    draft = draft_head()
    if not production or not draft:
        return {}
    return _rule_diff(production["rules"], draft["rules"])


def _draft_rules_with(rule_id: str, priority: int | None, enabled: bool) -> list[dict] | None:
    draft = draft_head()
    production = production_ruleset()
    if draft is None:
        return None
    rules = [dict(r) for r in draft["rules"]]
    base = {r["id"]: r for r in (production or {}).get("rules", [])}
    for rule in rules:
        if rule["id"] != rule_id:
            continue
        if priority is None:
            rule["priority"] = int((base.get(rule_id) or rule).get("priority", rule["priority"]))
        else:
            rule["priority"] = int(priority)
        rule["enabled"] = bool(enabled)
        return rules
    return None


def set_override(rule_id: str, priority: int | None, enabled: bool) -> dict:
    """旧接口：把一次优先级/启停调整写进新草稿修订（不再改生产版本）。"""
    rules = _draft_rules_with(rule_id, priority, enabled)
    if rules is None:
        raise KeyError(f"规则 {rule_id} 不存在")
    production = production_ruleset()
    base_version = production["version"] if production else 1
    draft = create_draft_revision(rules, base_version, "legacy_override",
                                  f"调整 {rule_id} 优先级/启停（旧接口）")
    return {"rule_id": rule_id, "priority": priority, "enabled": enabled,
            "draft_revision": draft["revision"]}


def reset_overrides() -> int:
    """旧接口：丢弃草稿差异，用生产版本重建草稿；返回被丢弃的差异条数。"""
    production = production_ruleset()
    draft = draft_head()
    if production is None:
        return 0
    removed = len(_rule_diff(production["rules"], draft["rules"])) if draft else 0
    if draft and draft["digest"] == production["digest"]:
        return 0
    create_draft_revision(production["rules"], production["version"], "reset",
                          "丢弃草稿改动，恢复为生产版本")
    return removed


def delete_override(rule_id: str) -> None:
    """旧接口：把单条规则恢复为生产版本中的值（生成新草稿修订）。"""
    production = production_ruleset()
    if production is None:
        return
    for rule in production["rules"]:
        if rule["id"] == rule_id:
            set_override(rule_id, int(rule.get("priority", 0)), bool(rule.get("enabled", True)))
            return


# --------------------------------------------------------------------------
# 源单据
# --------------------------------------------------------------------------

def list_docs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM source_docs ORDER BY doc_no").fetchall()
    return [
        {"doc_no": r["doc_no"], "version": r["version"], "doc": json.loads(r["payload_json"]), "seeded_at": r["seeded_at"]}
        for r in rows
    ]


def get_doc(doc_no: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM source_docs WHERE doc_no = ?", (doc_no,)).fetchone()
    if not row:
        return None
    return {"doc_no": row["doc_no"], "version": row["version"], "doc": json.loads(row["payload_json"])}


def count_docs() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM source_docs").fetchone()["c"]


# --------------------------------------------------------------------------
# 预览快照（锁定规则版本）
# --------------------------------------------------------------------------

def save_preview(run: dict, ruleset: dict | None = None) -> None:
    """保存预览快照；同一 run_id 重复预览只刷新结果与时间，不改变锁定内容。"""
    rules_json = (ruleset or {}).get("rules_json")
    if rules_json is None and run.get("rules_snapshot") is not None:
        rules_json = json.dumps(run["rules_snapshot"], ensure_ascii=False)
    ref = run.get("ruleset_ref") or (ruleset or {}).get("ref") or ""
    version = run.get("ruleset_version")
    kind = (ruleset or {}).get("kind") or run.get("ruleset_kind")
    if version is None and kind != "draft":
        version = (ruleset or {}).get("version")
    revision = run.get("draft_revision")
    if revision is None and kind == "draft":
        revision = (ruleset or {}).get("revision")
    with connect() as conn:
        conn.execute(
            "INSERT INTO previews (run_id, doc_no, version, rules_digest, ruleset_ref, "
            "ruleset_version, draft_revision, rules_json, result_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET result_json = excluded.result_json, "
            "created_at = excluded.created_at",
            (run["run_id"], run["doc_no"], run["version"], run["rules_digest"], ref,
             version, revision, rules_json, json.dumps(run, ensure_ascii=False), now()),
        )
        conn.commit()


def get_preview(run_id: str, with_rules: bool = False) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM previews WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        return None
    run = json.loads(row["result_json"])
    run["preview_created_at"] = row["created_at"]
    run["ruleset_ref"] = row["ruleset_ref"] or run.get("ruleset_ref", "")
    run["ruleset_version"] = row["ruleset_version"]
    run["draft_revision"] = row["draft_revision"]
    if with_rules:
        run["rules_snapshot"] = json.loads(row["rules_json"]) if row["rules_json"] else None
    return run


def list_previews(limit: int = 40) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT run_id, doc_no, version, ruleset_ref, ruleset_version, draft_revision, "
            "rules_digest, created_at FROM previews ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def preview_counts_by_digest() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT rules_digest, COUNT(*) AS c FROM previews GROUP BY rules_digest"
        ).fetchall()
    return {r["rules_digest"]: r["c"] for r in rows}


def _normalize_digest(digest: str | None) -> str:
    return (digest or "").lower()


# --------------------------------------------------------------------------
# 提交与回执
# --------------------------------------------------------------------------

def find_by_key(idem_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE idem_key = ?", (idem_key,)).fetchone()
    return _submission(row) if row else None


def find_doc_version(doc_no: str, version: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE doc_no = ? AND version = ?", (doc_no, int(version))
        ).fetchone()
    return _submission(row) if row else None


def latest_version(doc_no: str) -> int | None:
    with connect() as conn:
        row = conn.execute("SELECT MAX(version) AS v FROM submissions WHERE doc_no = ?", (doc_no,)).fetchone()
    return row["v"] if row and row["v"] is not None else None


def insert_submission(
    receipt_no: str,
    idem_key: str,
    doc_no: str,
    version: int,
    payload_json: str,
    payload_sha: str,
    ruleset_ref: str = "",
    ruleset_version: int | None = None,
    draft_revision: int | None = None,
    rules_digest: str = "",
    rules_json: str = "",
    run_id: str = "",
) -> dict:
    created = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO submissions (receipt_no, idem_key, doc_no, version, payload_json, payload_sha, "
            "ruleset_ref, ruleset_version, draft_revision, rules_digest, rules_json, run_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_no, idem_key, doc_no, int(version), payload_json, payload_sha,
             ruleset_ref, ruleset_version, draft_revision, rules_digest, rules_json, run_id, created),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _submission(row)


def add_audit(action: str, doc_no: str, version: int, detail: str = "", submission: dict | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events (submission_id, receipt_no, doc_no, version, action, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                submission["id"] if submission else None,
                submission["receipt_no"] if submission else None,
                doc_no,
                int(version),
                action,
                detail,
                now(),
            ),
        )
        conn.commit()


def _submission(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "receipt_no": row["receipt_no"],
        "idem_key": row["idem_key"],
        "doc_no": row["doc_no"],
        "version": row["version"],
        "payload": json.loads(row["payload_json"]),
        "payload_sha": row["payload_sha"],
        "ruleset_ref": row["ruleset_ref"],
        "ruleset_version": row["ruleset_version"],
        "draft_revision": row["draft_revision"],
        "rules_digest": row["rules_digest"],
        "rules_json": row["rules_json"],
        "run_id": row["run_id"],
        "created_at": row["created_at"],
    }


def list_submissions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM submissions ORDER BY doc_no, version").fetchall()
    return [_submission(r) for r in rows]


def get_submission(receipt_no: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE receipt_no = ?", (receipt_no,)).fetchone()
    return _submission(row) if row else None


def list_audits(limit: int = 40) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


def count_submissions() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM submissions").fetchone()["c"]


def replay_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT receipt_no, COUNT(*) AS c FROM audit_events WHERE action = 'replay' GROUP BY receipt_no"
        ).fetchall()
    return {r["receipt_no"]: r["c"] for r in rows}


def submission_counts_by_digest() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT rules_digest, COUNT(*) AS c FROM submissions WHERE rules_digest != '' "
            "GROUP BY rules_digest"
        ).fetchall()
    return {_normalize_digest(r["rules_digest"]): r["c"] for r in rows}


def submission_counts_by_revision() -> dict[int, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT draft_revision, COUNT(*) AS c FROM submissions "
            "WHERE draft_revision IS NOT NULL GROUP BY draft_revision"
        ).fetchall()
    return {int(r["draft_revision"]): r["c"] for r in rows}


def submission_counts_by_ref() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ruleset_ref, COUNT(*) AS c FROM submissions WHERE ruleset_ref != '' "
            "GROUP BY ruleset_ref"
        ).fetchall()
    return {r["ruleset_ref"]: r["c"] for r in rows}
