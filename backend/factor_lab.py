"""因子研究工作台：账号隔离的「研究 / 实验」持久化 + 单车道异步执行。

设计边界（探索级）：
- 复用 users 的 SQLite（users.DB_FILE），研究/实验各占一张新表；大型分析结果只落在
  这些表里，不进入前端整存整取的 user_data。
- 定义、研究配置、已完成结果均不可覆盖：改公式/参数只能新建实验。
- 所有读写都带 user_id 作用域；引用（基线/来源/冻结/结论/导出）同样校验归属。
- 计算下沉独立子进程（沿用 score_worker 的既有模式），单车道串行，避免同时加载多份
  全市场面板。子进程用「仅当仍为 queued/running 才落终态」的条件写，取消不会被覆盖。
- 服务重启后遗留的 queued/running 显式标记失败，原始定义与参数保留。

计算本身由 factor_analysis 提供（并行实现），本模块只在调用时才 import 它。
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager

import users

HERE = os.path.dirname(os.path.abspath(__file__))
TERMINAL = ("completed", "failed", "cancelled")
ACTIVE = ("queued", "running")
PHASES = ("development", "validation", "holdout")
DECISIONS = ("继续验证", "保留候选", "暂缓", "淘汰")
EXPORT_FORMAT = "market-workbench-factor-study"
EXPORT_VERSION = 1
MAX_RUNS_PER_STUDY = 200
MAX_PENDING = 40  # 单车道待跑上限：全市场计算昂贵，无界排队只会让用户等更久
WORKER_MODULE = "factor_lab_worker"

_RUN_TIMEOUT = 3600.0  # 单次全市场计算的上限；超时按失败处理


class FactorLabError(Exception):
    """路由层转 HTTP 400 的用户可读错误。"""


class FactorLabConflict(FactorLabError):
    """数据/状态已变化导致操作冲突 → HTTP 409。"""


def _analysis():
    """懒加载计算模块：import 时不触发，也便于测试注入替身。"""
    import factor_analysis

    return factor_analysis


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------

def init_db() -> None:
    with _db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS factor_studies (
                   id TEXT PRIMARY KEY,
                   user_id INTEGER NOT NULL,
                   name TEXT NOT NULL,
                   description TEXT NOT NULL,
                   config TEXT NOT NULL,
                   input_manifest TEXT NOT NULL,
                   baseline_id TEXT,
                   archived INTEGER NOT NULL DEFAULT 0,
                   freezes TEXT NOT NULL DEFAULT '[]',
                   holdout_used_at REAL,
                   decisions TEXT NOT NULL DEFAULT '[]',
                   created_at REAL NOT NULL,
                   updated_at REAL NOT NULL)"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_factor_studies_user ON factor_studies(user_id, updated_at DESC)"
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS factor_runs (
                   id TEXT PRIMARY KEY,
                   study_id TEXT NOT NULL,
                   user_id INTEGER NOT NULL,
                   kind TEXT NOT NULL,
                   name TEXT NOT NULL,
                   definition TEXT,
                   phase TEXT NOT NULL,
                   source_run_ids TEXT NOT NULL DEFAULT '[]',
                   params TEXT NOT NULL DEFAULT '{}',
                   input_manifest TEXT NOT NULL,
                   status TEXT NOT NULL,
                   error TEXT,
                   result TEXT,
                   created_at REAL NOT NULL,
                   started_at REAL,
                   finished_at REAL)"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_factor_runs_study ON factor_runs(study_id, created_at)"
        )


@contextmanager
def _db(*, write=False):
    conn = users._conn()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 序列化 / 行转换
# ---------------------------------------------------------------------------

def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _run_public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "study_id": row["study_id"],
        "kind": row["kind"],
        "name": row["name"],
        "definition": _loads(row["definition"], None),
        "phase": row["phase"],
        "source_run_ids": _loads(row["source_run_ids"], []),
        "params": _loads(row["params"], {}),
        "input_manifest": _loads(row["input_manifest"], {}),
        "status": row["status"],
        "error": row["error"],
        "result": _loads(row["result"], None),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _study_public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "config": _loads(row["config"], {}),
        "input_manifest": _loads(row["input_manifest"], {}),
        "baseline_id": row["baseline_id"],
        "archived": bool(row["archived"]),
        "freezes": _loads(row["freezes"], []),
        "holdout_used_at": row["holdout_used_at"],
        "decisions": _loads(row["decisions"], []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# ---------------------------------------------------------------------------
# 研究（Study）
# ---------------------------------------------------------------------------

def list_studies(uid: int) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM factor_studies WHERE user_id=? ORDER BY updated_at DESC", (uid,)
        ).fetchall()
    return [_study_public(r) for r in rows]


def get_study(uid: int, sid: str) -> dict | None:
    with _db() as c:
        row = c.execute(
            "SELECT * FROM factor_studies WHERE id=? AND user_id=?", (sid, uid)
        ).fetchone()
    return _study_public(row) if row else None


def get_study_by_id(sid: str) -> dict | None:
    """工作线程/子进程用：无账号作用域（调用方已持有可信研究 id）。"""
    with _db() as c:
        row = c.execute("SELECT * FROM factor_studies WHERE id=?", (sid,)).fetchone()
    return _study_public(row) if row else None


def _require_study(uid: int, sid: str, c=None) -> dict:
    if c is None:
        with _db() as c:
            return _require_study(uid, sid, c)
    row = c.execute("SELECT * FROM factor_studies WHERE id=? AND user_id=?", (sid, uid)).fetchone()
    if row is None:
        raise LookupError("研究不存在")
    return _study_public(row)


def _touch(c, sid: str, now: float) -> None:
    c.execute("UPDATE factor_studies SET updated_at=? WHERE id=?", (now, sid))


def create_study(uid: int, name: str, description: str, config: dict) -> dict:
    name = (name or "").strip()
    if not name:
        raise FactorLabError("研究名称不能为空")
    if len(name) > 80:
        raise FactorLabError("研究名称不能超过 80 字")
    description = (description or "").strip()
    if len(description) > 500:
        raise FactorLabError("研究说明不能超过 500 字")
    try:
        normalized = _analysis().validate_config(config or {})
    except FactorLabError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise FactorLabError(f"研究配置无效：{exc}") from exc
    manifest = _analysis().input_manifest()
    now = time.time()
    sid = _new_id("st")
    with _db() as c:
        c.execute(
            """INSERT INTO factor_studies
               (id, user_id, name, description, config, input_manifest, baseline_id, archived,
                freezes, holdout_used_at, decisions, created_at, updated_at)
               VALUES (?,?,?,?,?,?,NULL,0,'[]',NULL,'[]',?,?)""",
            (sid, uid, name, description, _dumps(normalized), _dumps(manifest), now, now),
        )
    return _require_study(uid, sid)


def update_study(uid: int, sid: str, *, name=None, description=None, archived=None,
                 baseline_id=...) -> dict:
    """只改元数据；配置与输入快照不可覆盖。"""
    study = _require_study(uid, sid)
    updates: dict = {}
    if name is not None:
        name = str(name).strip()
        if not name:
            raise FactorLabError("研究名称不能为空")
        if len(name) > 80:
            raise FactorLabError("研究名称不能超过 80 字")
        updates["name"] = name
    if description is not None:
        description = str(description).strip()
        if len(description) > 500:
            raise FactorLabError("研究说明不能超过 500 字")
        updates["description"] = description
    if archived is not None:
        updates["archived"] = 1 if archived else 0
    if baseline_id is not ...:
        if baseline_id is None:
            updates["baseline_id"] = None
        else:
            run = _get_run(uid, sid, str(baseline_id))
            if not run:
                raise LookupError("基线实验不存在")
            if run["kind"] != "evaluate" or run["status"] != "completed" or run["phase"] != "development":
                raise FactorLabConflict("基线必须是已完成的开发期检验")
            updates["baseline_id"] = run["id"]
    if not updates:
        return study
    now = time.time()
    with _db(write=True) as c:
        c.execute(
            """UPDATE factor_studies SET name=COALESCE(?, name), description=COALESCE(?, description),
               archived=COALESCE(?, archived), baseline_id=CASE WHEN ? THEN ? ELSE baseline_id END,
               updated_at=? WHERE id=? AND user_id=?""",
            (updates.get("name"), updates.get("description"), updates.get("archived"),
             "baseline_id" in updates, updates.get("baseline_id"), now, sid, uid),
        )
    return _require_study(uid, sid)


# ---------------------------------------------------------------------------
# 实验（Run）
# ---------------------------------------------------------------------------

def _get_run(uid: int, sid: str, rid: str, c=None) -> dict | None:
    if c is None:
        with _db() as c:
            return _get_run(uid, sid, rid, c)
    row = c.execute(
        "SELECT * FROM factor_runs WHERE id=? AND study_id=? AND user_id=?", (rid, sid, uid)
    ).fetchone()
    return _run_public(row) if row else None


def get_run(uid: int, sid: str, rid: str) -> dict | None:
    return _get_run(uid, sid, rid)


def get_run_by_id(rid: str) -> dict | None:
    """工作线程/子进程用：无账号作用域（调用方已持有可信 run id）。"""
    with _db() as c:
        row = c.execute("SELECT * FROM factor_runs WHERE id=?", (rid,)).fetchone()
    return _run_public(row) if row else None


def list_runs(uid: int, sid: str) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM factor_runs WHERE study_id=? AND user_id=? ORDER BY created_at",
            (sid, uid),
        ).fetchall()
    return [_run_public(r) for r in rows]


def study_payload(uid: int, sid: str) -> dict | None:
    study = get_study(uid, sid)
    if not study:
        return None
    return {"study": study, "runs": list_runs(uid, sid)}


def _insert_run(uid: int, sid: str, *, kind: str, name: str, definition, phase: str,
                source_run_ids: list[str], params: dict, manifest: dict, c=None) -> dict:
    if c is None:
        with _db(write=True) as c:
            return _insert_run(uid, sid, kind=kind, name=name, definition=definition, phase=phase,
                               source_run_ids=source_run_ids, params=params, manifest=manifest, c=c)
    study = _require_study(uid, sid, c)
    if study["archived"]:
        raise FactorLabConflict("研究已归档")
    name = (name or "").strip()
    if not name:
        raise FactorLabError("实验名称不能为空")
    if len(name) > 80:
        raise FactorLabError("实验名称不能超过 80 字")
    active = c.execute(
        "SELECT COUNT(*) FROM factor_runs WHERE status IN ('queued','running')"
    ).fetchone()[0]
    if active >= MAX_PENDING:
        raise FactorLabConflict("任务队列已满，请稍后再试")
    count = c.execute("SELECT COUNT(*) FROM factor_runs WHERE study_id=? AND user_id=?", (sid, uid)).fetchone()[0]
    if count >= MAX_RUNS_PER_STUDY:
        raise FactorLabConflict("实验数量已达上限，请新建研究")
    now = time.time()
    rid = _new_id("run")
    c.execute(
        """INSERT INTO factor_runs
           (id, study_id, user_id, kind, name, definition, phase, source_run_ids, params,
            input_manifest, status, error, result, created_at, started_at, finished_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,'queued',NULL,NULL,?,NULL,NULL)""",
        (rid, sid, uid, kind, name, _dumps(definition) if definition is not None else None,
         phase, _dumps(source_run_ids), _dumps(params), _dumps(manifest), now),
    )
    _touch(c, sid, now)
    run = _get_run(uid, sid, rid, c)
    assert run is not None
    return run

def _require_source(uid: int, sid: str, source_run_id: str | None, study: dict, c) -> dict:
    """验证/留出期只能复用冻结选择里的、已完成的开发期来源实验。"""
    if not source_run_id:
        raise FactorLabError("验证/留出期实验必须提供来源实验")
    source = _get_run(uid, sid, source_run_id, c)
    if not source:
        raise LookupError("来源实验不存在")
    if source["kind"] != "evaluate" or source["phase"] != "development" or source["status"] != "completed":
        raise FactorLabConflict("来源必须是已完成的开发期检验")
    frozen = any(source["id"] in (fz.get("run_ids") or []) for fz in study["freezes"])
    if not frozen:
        raise FactorLabConflict("请先锁定来源实验")
    return source


def create_run(uid: int, sid: str, *, name: str, definition=None, phase: str | None = None,
               source_run_id: str | None = None) -> dict:
    _require_study(uid, sid)
    phase = "development" if phase is None else phase
    if phase not in PHASES:
        raise FactorLabError("请选择开发、验证或留出期")
    normalized = None
    if phase == "development":
        if source_run_id is not None:
            raise FactorLabError("开发期不能指定来源实验")
        if not isinstance(definition, dict) or set(definition) not in (
                {"factor", "direction"}, {"expr", "direction"}):
            raise FactorLabError("请提供单一因子定义和方向")
        if str(definition.get("factor", "")).startswith("custom:"):
            raise FactorLabError("自定义因子请提交公式快照")
        try:
            normalized = _analysis().validate_definition(definition)
        except (ValueError, KeyError, TypeError) as exc:
            raise FactorLabError("因子定义无效") from exc
    elif definition is not None:
        raise FactorLabError("验证/留出期不能另传定义")
    manifest = _analysis().input_manifest()
    with _db(write=True) as c:
        study = _require_study(uid, sid, c)
        source_ids = []
        if phase != "development":
            source = _require_source(uid, sid, source_run_id, study, c)
            if source["input_manifest"] != manifest:
                raise FactorLabConflict("输入数据已变化，请重跑开发期")
            normalized = source["definition"]
            source_ids = [source["id"]]
            manifest = source["input_manifest"]
        if normalized is None:
            raise FactorLabError("因子定义无效")
        run = _insert_run(uid, sid, kind="evaluate", name=name, definition=normalized,
                          phase=phase, source_run_ids=source_ids, params={}, manifest=manifest, c=c)
        if phase == "holdout":
            # 校验和入队同一事务；失败/取消/重试都不能抹掉首次使用时间。
            c.execute("""UPDATE factor_studies SET holdout_used_at=COALESCE(holdout_used_at, ?)
                         WHERE id=? AND user_id=?""", (time.time(), sid, uid))
        return run

def create_compare(uid: int, sid: str, name: str, run_ids: list[str]) -> dict:
    study = _require_study(uid, sid)
    if study["archived"]:
        raise FactorLabConflict("研究已归档")
    if not isinstance(run_ids, (list, tuple)):
        raise FactorLabError("run_ids 必须是数组")
    ids = [str(r) for r in run_ids]
    if len(ids) != len(set(ids)):
        raise FactorLabError("对比的实验必须互不相同")
    if not 2 <= len(ids) <= 4:
        raise FactorLabError("对比需要 2-4 个已完成的检验实验")
    runs = []
    for rid in ids:
        run = _get_run(uid, sid, rid)
        if not run:
            raise LookupError("对比的实验不存在")
        if run["kind"] != "evaluate" or run["status"] != "completed":
            raise FactorLabConflict("对比需要已完成的检验实验")
        runs.append(run)
    phases = {r["phase"] for r in runs}
    if len(phases) != 1:
        raise FactorLabError("对比的实验必须处于同一阶段")
    manifests = {_dumps(r["input_manifest"]) for r in runs}
    if len(manifests) != 1:
        raise FactorLabError("对比的实验输入数据不一致，请先重跑")
    manifest = runs[0]["input_manifest"]
    return _insert_run(uid, sid, kind="compare", name=name, definition=None,
                       phase=runs[0]["phase"], source_run_ids=ids, params={}, manifest=manifest)


def create_backtest(uid: int, sid: str, name: str, run_id: str, top_n=None, freq=None,
                    cost=None) -> dict:
    study = _require_study(uid, sid)
    if study["archived"]:
        raise FactorLabConflict("研究已归档")
    source = _get_run(uid, sid, str(run_id))
    if not source:
        raise LookupError("来源实验不存在")
    if source["kind"] != "evaluate" or source["status"] != "completed":
        raise FactorLabConflict("回测需要已完成的检验实验")
    params: dict = {"top_n": 50, "freq": "monthly", "cost": 1.0}
    if top_n is not None:
        try:
            if isinstance(top_n, bool) or not isinstance(top_n, int):
                raise ValueError
        except (TypeError, ValueError):
            raise FactorLabError("top_n 必须是整数") from None
        if not 1 <= top_n <= 500:
            raise FactorLabError("持仓数需为 1-500")
        params["top_n"] = top_n
    if freq is not None:
        if freq not in ("weekly", "monthly"):
            raise FactorLabError("freq 必须是 weekly/monthly")
        params["freq"] = freq
    if cost is not None:
        try:
            if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                raise ValueError
            cost = float(cost)
        except (TypeError, ValueError):
            raise FactorLabError("cost 必须是数字") from None
        if not math.isfinite(cost) or not 0 <= cost <= 3:
            raise FactorLabError("成本倍数需为 0-3")
        params["cost"] = cost
    manifest = source["input_manifest"]
    return _insert_run(uid, sid, kind="backtest", name=name, definition=source["definition"],
                       phase=source["phase"], source_run_ids=[source["id"]], params=params,
                       manifest=manifest)


def freeze_study(uid: int, sid: str, run_ids: list[str]) -> dict:
    with _db(write=True) as c:
        study = _require_study(uid, sid, c)
        if study["archived"]:
            raise FactorLabConflict("研究已归档")
        if not isinstance(run_ids, list) or not run_ids:
            raise FactorLabError("请选择要锁定的实验")
        ids = list(run_ids)
        if any(not isinstance(rid, str) for rid in ids) or len(ids) != len(set(ids)):
            raise FactorLabError("锁定的实验必须互不相同")
        for rid in ids:
            if not _get_run(uid, sid, rid, c):
                raise LookupError("实验不存在")
        baseline_id = study["baseline_id"]
        if not baseline_id:
            raise FactorLabConflict("请先设置基线实验")
        if baseline_id not in ids:
            ids.insert(0, baseline_id)
        if len(ids) > 4:
            raise FactorLabError("含基线最多锁定 4 个实验")
        for rid in ids:
            run = _get_run(uid, sid, rid, c)
            if not run:
                raise LookupError("实验不存在")
            if run["kind"] != "evaluate" or run["status"] != "completed" or run["phase"] != "development":
                raise FactorLabConflict("只能锁定已完成的开发期检验")
        now = time.time()
        entry = {"id": _new_id("fz"), "run_ids": ids, "baseline_id": baseline_id, "created_at": now}
        freezes = study["freezes"] + [entry]
        c.execute("UPDATE factor_studies SET freezes=?, updated_at=? WHERE id=? AND user_id=?",
                  (_dumps(freezes), now, sid, uid))
        return _require_study(uid, sid, c)

def add_decision(uid: int, sid: str, decision: str, note: str, run_ids=None) -> dict:
    with _db(write=True) as c:
        study = _require_study(uid, sid, c)
        if decision not in DECISIONS:
            raise FactorLabError("请选择继续验证、保留候选、暂缓或淘汰")
        note = (note or "").strip()
        if not note:
            raise FactorLabError("备注不能为空")
        if len(note) > 1000:
            raise FactorLabError("备注不能超过 1000 字")
        ids = [] if run_ids is None else run_ids
        if not isinstance(ids, list) or any(not isinstance(rid, str) for rid in ids):
            raise FactorLabError("实验引用必须是数组")
        for rid in ids:
            if not _get_run(uid, sid, rid, c):
                raise LookupError("引用的实验不存在")
        now = time.time()
        entry = {"decision": decision, "note": note, "run_ids": ids, "created_at": now}
        decisions = study["decisions"] + [entry]
        c.execute("UPDATE factor_studies SET decisions=?, updated_at=? WHERE id=? AND user_id=?",
                  (_dumps(decisions), now, sid, uid))
        return _require_study(uid, sid, c)

def export_study(uid: int, sid: str) -> dict | None:
    study = get_study(uid, sid)
    if not study:
        return None
    runs = [_exportable_run(r) for r in list_runs(uid, sid)]
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "study": _strip_local_paths(study),
        "runs": runs,
    }


def _strip_local_paths(obj):
    """导出元数据只保留逻辑标签：抹掉 manifest.files 里的本机绝对路径。"""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "files" and isinstance(value, dict):
                out[key] = {
                    k: ({"logical": os.path.basename(v.replace("\\", "/"))} if isinstance(v, str)
                        else _logical_paths(v))
                    for k, v in value.items()
                }
            else:
                out[key] = _strip_local_paths(value)
        return out
    if isinstance(obj, list):
        return [_strip_local_paths(v) for v in obj]
    return obj


def _logical_paths(obj):
    if isinstance(obj, dict):
        return {key: _logical_paths(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_logical_paths(value) for value in obj]
    if isinstance(obj, str) and (os.path.isabs(obj) or (len(obj) > 2 and obj[1:3] in (":\\", ":/"))):
        return os.path.basename(obj.replace("\\", "/"))
    return obj


def _exportable_run(run: dict) -> dict:
    return {
        "id": run["id"],
        "study_id": run["study_id"],
        "kind": run["kind"],
        "name": run["name"],
        "definition": run["definition"],
        "phase": run["phase"],
        "source_run_ids": run["source_run_ids"],
        "params": run["params"],
        "input_manifest": _strip_local_paths(run["input_manifest"]),
        "status": run["status"],
        "error": run["error"],
        "result": _strip_local_paths(run["result"]),
        "created_at": run["created_at"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
    }


# ---------------------------------------------------------------------------
# 终态写入（子进程与父进程共用；条件更新保证取消不被覆盖）
# ---------------------------------------------------------------------------

def finish_run(rid: str, *, status: str, error=None, result=None) -> bool:
    """仅当实验仍处于 queued/running 时写入终态，返回是否写入成功。"""
    if status not in TERMINAL:
        raise ValueError(f"非法终态 {status}")
    now = time.time()
    with _db() as c:
        cur = c.execute(
            """UPDATE factor_runs SET status=?, error=?, result=?, finished_at=?
                WHERE id=? AND status IN ('queued','running')""",
            (status, error, _dumps(result) if result is not None else None, now, rid),
        )
        return cur.rowcount > 0


def cancel_run(uid: int, sid: str, rid: str) -> dict | None:
    with _db(write=True) as c:
        run = _get_run(uid, sid, rid, c)
        if run is None:
            return None
        changed = c.execute(
            """UPDATE factor_runs SET status='cancelled', error=NULL, result=NULL, finished_at=?
               WHERE id=? AND study_id=? AND user_id=? AND status IN ('queued','running')""",
            (time.time(), rid, sid, uid),
        ).rowcount
        run = _get_run(uid, sid, rid, c)
    if changed:
        _write_cancel_flag(rid)
    return run

def is_cancelled(rid: str) -> bool:
    if os.path.exists(_cancel_flag(rid)):
        return True
    with _db() as c:
        row = c.execute("SELECT status FROM factor_runs WHERE id=?", (rid,)).fetchone()
    return row is None or row["status"] not in ACTIVE


def _cancel_flag(rid: str) -> str:
    # 取消标记跟着 users.db 走：测试/多库场景下不会串到别的库的取消记录。
    directory = os.path.join(os.path.dirname(os.path.abspath(users.DB_FILE)), "factor-lab-cancel")
    return os.path.join(directory, f"{rid}.cancel")


def _write_cancel_flag(rid: str) -> None:
    try:
        os.makedirs(os.path.dirname(_cancel_flag(rid)), exist_ok=True)
        with open(_cancel_flag(rid), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def recover_abandoned() -> int:
    """服务重启后把遗留的 queued/running 显式标为失败（保留原始定义与参数）。"""
    now = time.time()
    with _db() as c:
        cur = c.execute(
            """UPDATE factor_runs SET status='failed', error='服务重启，任务已中断', finished_at=?
                WHERE status IN ('queued','running')""",
            (now,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# 单车道队列 + 子进程执行
# ---------------------------------------------------------------------------

_QUEUE: deque[str] = deque()
_QUEUE_LOCK = threading.Lock()
_EXECUTION_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STOPPING = threading.Event()
_STARTED = False


def _take() -> str | None:
    with _QUEUE_LOCK:
        return _QUEUE.popleft() if _QUEUE and not _STOPPING.is_set() else None


def worker_main() -> None:
    """单车道循环：一次只跑一个计算子进程，避免并发加载多份全市场面板。"""
    global _THREAD
    while True:
        rid = _take()
        if rid is None:
            with _QUEUE_LOCK:
                if _QUEUE:  # submit 与出队竞态：队列又非空则继续
                    continue
                _THREAD = None
                return
        try:
            _execute(rid)
        except Exception:  # noqa: BLE001 - 单个任务失败不能拖垮车道
            try:
                finish_run(rid, status="failed", error="任务执行异常")
            except Exception as exc:  # noqa: BLE001
                print(f"[factor-lab] 任务状态写入失败：{exc}", file=sys.stderr, flush=True)


def _ensure_worker() -> None:
    global _THREAD
    with _QUEUE_LOCK:
        if not _STOPPING.is_set() and (_THREAD is None or not _THREAD.is_alive()):
            _THREAD = threading.Thread(target=worker_main, daemon=True, name="factor-lab-worker")
            _THREAD.start()


def submit(rid: str) -> None:
    with _QUEUE_LOCK:
        stopping = _STOPPING.is_set()
        if not stopping:
            _QUEUE.append(rid)
    if stopping:
        finish_run(rid, status="failed", error="服务停止，任务已中断")
        raise FactorLabConflict("服务停止中，请稍后再试")
    _ensure_worker()


def _execute(rid: str) -> None:
    # Unix lane lock is inherited by the child: an orphan keeps the memory lane reserved.
    lock_file = os.path.join(os.path.dirname(os.path.abspath(users.DB_FILE)), "factor-lab-worker.lock")
    try:
        with _EXECUTION_LOCK, open(lock_file, "a+") as lane:
            # Shutdown must also release a lane thread waiting behind an orphan child.
            while not _STOPPING.is_set():
                try:
                    fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _STOPPING.wait(0.05)
            if not _STOPPING.is_set():
                _execute_locked(rid, lane.fileno())
    except OSError:
        finish_run(rid, status="failed", error="计算环境不可用")


def _execute_locked(rid: str, lane_fd: int) -> None:
    if _STOPPING.is_set():
        return
    with _db(write=True) as c:
        claimed = c.execute(
            "UPDATE factor_runs SET status='running', started_at=? WHERE id=? AND status='queued'",
            (time.time(), rid),
        ).rowcount
    if not claimed:
        return
    if _STOPPING.is_set():
        finish_run(rid, status="failed", error="服务停止，任务已中断")
        return
    # argv is data, never Python code or shell input; stderr goes to disk to avoid pipe deadlocks.
    cmd = [sys.executable, "-m", WORKER_MODULE, users.DB_FILE, rid]
    with tempfile.TemporaryFile(mode="w+") as log:
        try:
            proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.DEVNULL, stderr=log,
                                    pass_fds=(lane_fd,))
        except OSError:
            finish_run(rid, status="failed", error="计算进程启动失败")
            return
        deadline = time.monotonic() + _RUN_TIMEOUT
        try:
            while proc.poll() is None:
                if is_cancelled(rid):
                    _stop(proc)
                    finish_run(rid, status="cancelled")
                    break
                if time.monotonic() > deadline:
                    _stop(proc)
                    finish_run(rid, status="failed", error="计算超时，已终止")
                    break
                time.sleep(0.2)
            proc.wait()
        finally:
            if proc.poll() is None:
                _stop(proc)
        log.seek(0)
        err = log.read(4000).strip()
        if err:
            print(f"[{WORKER_MODULE}] {rid}: {err}", file=sys.stderr, flush=True)
        error = "计算进程异常退出" if proc.returncode else "计算未返回结果"
        finish_run(rid, status="failed", error=error)

def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def start_worker() -> None:
    """服务 supervisor 启动时调用；必须先 init_db，再接受请求。"""
    global _STARTED
    with _QUEUE_LOCK:
        if _STARTED:
            return
        recover_abandoned()
        _STOPPING.clear()
        _STARTED = True


def stop_worker() -> None:
    """ASGI finally 调用；中断活动任务并等待子进程退出，再允许切换数据库。"""
    global _STARTED, _THREAD
    with _QUEUE_LOCK:
        active = _STARTED or _THREAD is not None or bool(_QUEUE)
        _STOPPING.set()
        _QUEUE.clear()
        thread = _THREAD
    if not active:
        return
    with _db(write=True) as c:
        c.execute(
            """UPDATE factor_runs SET status='failed', error='服务停止，任务已中断', finished_at=?
               WHERE status IN ('queued','running')""", (time.time(),),
        )
    # The lane polls the persisted terminal state and terminates/reaps its child.
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=15)
        if thread.is_alive():
            raise RuntimeError("因子任务未能停止")
    with _QUEUE_LOCK:
        _THREAD = None
        _STARTED = False
