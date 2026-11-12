"""因子研究工作台：持久化 + 子进程 worker 回归（不联网）。

计算模块 factor_analysis 由并行 agent 实现；这里的领域/生命周期测试用替身注入，
真实的端到端计算放在 test_real_worker_compute_integration，不允许以跳过代替执行。
"""

import json
import os
import subprocess
import sys
import types

import pytest

import factor_lab
import users

# ---------------------------------------------------------------------------
# 替身计算模块（形状对齐 architecture.md 因子研究接口契约的输入校验）
# ---------------------------------------------------------------------------

class FakeAnalysis:
    def __init__(self, data_version: str = "dv1"):
        self.data_version = data_version

    def input_manifest(self) -> dict:
        return {
            "id": f"man-{self.data_version}",
            "data_version": self.data_version,
            "files": {"bars": "/Users/x/.market-workbench/factor/bars.csv.gz",
                      "catalog": "/Users/x/.market-workbench/factor/catalog.json"},
            "code": {"factors.py": "deadbeef"},
        }

    def validate_definition(self, definition: dict) -> dict:
        if not isinstance(definition, dict):
            raise ValueError("因子定义必须是对象")
        factor, expr = definition.get("factor"), definition.get("expr")
        if bool(factor) == bool(expr):
            raise ValueError("因子定义必须且只能包含 factor 或 expr 之一")
        direction = definition.get("direction")
        if direction not in ("high", "low"):
            raise ValueError("direction 必须是 high/low")
        return {"factor": str(factor), "direction": direction} if factor else \
               {"expr": str(expr), "direction": direction}

    def validate_config(self, config: dict) -> dict:
        horizon = config.get("horizon")
        if horizon not in (1, 5, 10, 20):
            raise ValueError("horizon 必须是 1/5/10/20")
        min_days = config.get("min_days_listed")
        if not isinstance(min_days, int) or min_days < 0:
            raise ValueError("min_days_listed 必须是非负整数")
        periods = {}
        for key in ("development", "validation", "holdout"):
            period = config.get(key)
            if not isinstance(period, dict):
                raise ValueError(f"缺少 {key} 区间")
            start, end = period.get("start"), period.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                raise ValueError("日期需为 YYYY-MM-DD 格式")
            for value in (start, end):
                if not isinstance(value, str) or len(value) != 10 or value[4] != "-":
                    raise ValueError("日期需为 YYYY-MM-DD 格式")
            if start >= end:
                raise ValueError(f"{key} 起始日期需早于结束日期")
            periods[key] = (start, end)
        ordered = [periods["development"], periods["validation"], periods["holdout"]]
        if not (ordered[0][1] < ordered[1][0] and ordered[1][1] < ordered[2][0]):
            raise ValueError("三个区间必须不重叠且按顺序")
        return {"horizon": horizon, "min_days_listed": min_days,
                "development": config["development"], "validation": config["validation"],
                "holdout": config["holdout"]}


def _valid_config() -> dict:
    return {
        "horizon": 5,
        "min_days_listed": 20,
        "development": {"start": "2023-01-02", "end": "2023-06-30"},
        "validation": {"start": "2023-07-03", "end": "2023-09-29"},
        "holdout": {"start": "2023-10-02", "end": "2023-12-29"},
    }


@pytest.fixture
def lab(tmp_path, monkeypatch):
    factor_lab.stop_worker()
    monkeypatch.setattr(users, "DB_FILE", str(tmp_path / "users.db"))
    users.init_db()
    factor_lab.init_db()
    factor_lab.start_worker()
    monkeypatch.setattr(factor_lab, "_analysis", lambda: FakeAnalysis())
    try:
        yield factor_lab
    finally:
        factor_lab.stop_worker()


def _completed_run(lab, uid, sid, name="dev", factor="mom20"):
    run = lab.create_run(uid, sid, name=name, definition={"factor": factor, "direction": "high"})
    lab.finish_run(run["id"], status="completed", result={"phase": run["phase"], "native": [{}],
                                                          "horizons": [1, 5, 10, 20]})
    return lab.get_run(uid, sid, run["id"])


def _study(lab, uid=1, name="研究A"):
    return lab.create_study(uid, name, "说明", _valid_config())


# ---------------------------------------------------------------------------
# 研究与账号隔离
# ---------------------------------------------------------------------------

def test_studies_are_account_scoped(lab):
    study = _study(lab, uid=1)
    assert lab.list_studies(1)[0]["id"] == study["id"]
    assert lab.list_studies(2) == []
    assert lab.get_study(2, study["id"]) is None
    assert lab.study_payload(2, study["id"]) is None
    with pytest.raises(LookupError):
        lab.create_run(2, study["id"], name="x", definition={"factor": "mom20", "direction": "high"})


def test_config_is_validated_and_immutable(lab):
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_study(1, "坏配置", "", {**_valid_config(), "horizon": 3})
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_study(1, "坏区间", "", {**_valid_config(),
                                           "holdout": {"start": "2023-01-01", "end": "2023-02-01"}})
    study = _study(lab)
    updated = lab.update_study(1, study["id"], name="改名")
    assert updated["name"] == "改名"
    assert updated["config"] == study["config"]  # 配置永不被覆盖
    assert updated["input_manifest"] == study["input_manifest"]


def test_development_requires_definition_and_normalizes(lab):
    study = _study(lab)
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_run(1, study["id"], name="无定义")
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_run(1, study["id"], name="双定义",
                       definition={"factor": "a", "expr": "close", "direction": "high"})
    run = lab.create_run(1, study["id"], name="ok",
                         definition={"factor": "mom20", "direction": "high"})
    assert run["status"] == "queued"
    assert run["definition"] == {"factor": "mom20", "direction": "high"}
    assert run["input_manifest"]["id"] == "man-dv1"


# ---------------------------------------------------------------------------
# 冻结 / 验证 / 留出
# ---------------------------------------------------------------------------

def test_validation_requires_frozen_source(lab):
    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_run(1, study["id"], name="v", phase="validation", source_run_id=dev["id"])
    with pytest.raises(factor_lab.FactorLabError):
        lab.freeze_study(1, study["id"], [dev["id"]])  # 未设基线
    lab.update_study(1, study["id"], baseline_id=dev["id"])
    frozen = lab.freeze_study(1, study["id"], [dev["id"]])
    assert frozen["freezes"][0]["run_ids"] == [dev["id"]]
    assert frozen["freezes"][0]["baseline_id"] == dev["id"]
    validation = lab.create_run(1, study["id"], name="v", phase="validation", source_run_id=dev["id"])
    assert validation["phase"] == "validation"
    assert validation["definition"] == dev["definition"]  # 沿用来源定义，不可另传
    assert validation["source_run_ids"] == [dev["id"]]
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_run(1, study["id"], name="v2", phase="validation",
                       source_run_id=dev["id"], definition={"factor": "x", "direction": "low"})


def test_freeze_appends_and_includes_baseline(lab):
    study = _study(lab)
    base = _completed_run(lab, 1, study["id"], name="base")
    extra = _completed_run(lab, 1, study["id"], name="extra", factor="mom60")
    lab.update_study(1, study["id"], baseline_id=base["id"])
    first = lab.freeze_study(1, study["id"], [extra["id"]])
    assert first["freezes"][0]["run_ids"] == [base["id"], extra["id"]]  # 基线自动补入
    second = lab.freeze_study(1, study["id"], [base["id"]])
    assert len(second["freezes"]) == 2  # 追加，不覆盖
    assert second["freezes"][0] == first["freezes"][0]


def test_holdout_marker_is_permanent_and_retry_creates_new_run(lab):
    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    lab.update_study(1, study["id"], baseline_id=dev["id"])
    lab.freeze_study(1, study["id"], [dev["id"]])
    holdout = lab.create_run(1, study["id"], name="h", phase="holdout", source_run_id=dev["id"])
    study2 = lab.get_study(1, study["id"])
    assert study2["holdout_used_at"] is not None
    lab.cancel_run(1, study["id"], holdout["id"])
    retry = lab.create_run(1, study["id"], name="h2", phase="holdout", source_run_id=dev["id"])
    assert retry["id"] != holdout["id"]
    # 已提交的留出期实验仍在，且使用记录不会被清掉
    assert lab.get_run(1, study["id"], holdout["id"])["status"] == "cancelled"
    assert lab.get_study(1, study["id"])["holdout_used_at"] == study2["holdout_used_at"]


# ---------------------------------------------------------------------------
# 对比 / 回测 / 结论
# ---------------------------------------------------------------------------

def test_compare_rules(lab, monkeypatch):
    study = _study(lab)
    a = _completed_run(lab, 1, study["id"], name="a")
    b = _completed_run(lab, 1, study["id"], name="b", factor="mom60")
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_compare(1, study["id"], "c", [a["id"]])
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_compare(1, study["id"], "c", [a["id"], a["id"]])
    compare = lab.create_compare(1, study["id"], "c", [a["id"], b["id"]])
    assert compare["kind"] == "compare"
    assert compare["source_run_ids"] == [a["id"], b["id"]]
    # 不同数据版本的实验不得直接对比
    monkeypatch.setattr(factor_lab, "_analysis", lambda: FakeAnalysis("dv2"))
    other = _completed_run(lab, 1, study["id"], name="other")
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_compare(1, study["id"], "c2", [a["id"], other["id"]])


def test_backtest_reuses_definition_and_validates_params(lab):
    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    bt = lab.create_backtest(1, study["id"], "bt", dev["id"], top_n=20, freq="weekly", cost=1.5)
    assert bt["kind"] == "backtest"
    assert bt["definition"] == dev["definition"]
    assert bt["phase"] == dev["phase"]
    assert bt["params"] == {"top_n": 20, "freq": "weekly", "cost": 1.5}
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_backtest(1, study["id"], "bt2", dev["id"], freq="daily")
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_backtest(1, study["id"], "bt3", dev["id"], top_n=0)


def test_decisions_append_and_validate_references(lab):
    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    with pytest.raises(factor_lab.FactorLabError):
        lab.add_decision(1, study["id"], "继续验证", "   ")
    with pytest.raises(factor_lab.FactorLabError):
        lab.add_decision(1, study["id"], "通过", "note")
    with pytest.raises(LookupError):
        lab.add_decision(1, study["id"], "暂缓", "note", ["run_missing"])
    first = lab.add_decision(1, study["id"], "保留候选", "先保留", [dev["id"]])
    second = lab.add_decision(1, study["id"], "淘汰", "不达标")
    assert [d["decision"] for d in second["decisions"]] == ["保留候选", "淘汰"]
    assert first["decisions"][0]["run_ids"] == [dev["id"]]


def test_compare_requires_same_phase(lab):
    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    lab.update_study(1, study["id"], baseline_id=dev["id"])
    lab.freeze_study(1, study["id"], [dev["id"]])
    validation = lab.create_run(1, study["id"], name="v", phase="validation", source_run_id=dev["id"])
    lab.finish_run(validation["id"], status="completed", result={"native": [{}]})
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_compare(1, study["id"], "c", [dev["id"], validation["id"]])


def test_queue_and_size_bounds(lab, monkeypatch):
    study = _study(lab)
    monkeypatch.setattr(factor_lab, "MAX_PENDING", 1)
    lab.create_run(1, study["id"], name="a", definition={"factor": "mom20", "direction": "high"})
    with pytest.raises(factor_lab.FactorLabConflict):
        lab.create_run(1, study["id"], name="b", definition={"factor": "mom20", "direction": "high"})
    monkeypatch.setattr(factor_lab, "MAX_PENDING", 40)
    monkeypatch.setattr(factor_lab, "MAX_RUNS_PER_STUDY", 2)
    lab.create_run(1, study["id"], name="c", definition={"factor": "mom20", "direction": "high"})
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_run(1, study["id"], name="d", definition={"factor": "mom20", "direction": "high"})


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def test_export_is_scoped_and_strips_local_paths(lab):
    study = _study(lab, uid=1)
    dev = _completed_run(lab, 1, study["id"])
    export = lab.export_study(1, study["id"])
    assert export["format"] == "market-workbench-factor-study"
    assert export["version"] == 1
    assert export["study"]["id"] == study["id"]
    assert [r["id"] for r in export["runs"]] == [dev["id"]]
    text = json.dumps(export, ensure_ascii=False)
    assert "/Users/x/.market-workbench" not in text  # 不导出本机绝对路径
    assert export["study"]["input_manifest"]["files"]["bars"]["logical"] == "bars.csv.gz"
    assert lab.export_study(2, study["id"]) is None


# ---------------------------------------------------------------------------
# 取消 / 重启恢复 / 条件终态写
# ---------------------------------------------------------------------------

def test_cancel_is_not_overwritten_by_late_completion(lab):
    study = _study(lab)
    run = lab.create_run(1, study["id"], name="r", definition={"factor": "mom20", "direction": "high"})
    cancelled = lab.cancel_run(1, study["id"], run["id"])
    assert cancelled["status"] == "cancelled"
    assert lab.is_cancelled(run["id"])  # 取消标记已落盘，供子进程轮询
    assert lab.finish_run(run["id"], status="completed", result={"native": [{}]}) is False
    assert lab.get_run(1, study["id"], run["id"])["status"] == "cancelled"
    assert lab.cancel_run(1, study["id"], run["id"])["status"] == "cancelled"
    assert lab.cancel_run(1, study["id"], "run_missing") is None


def test_recover_abandoned_marks_failed_and_keeps_specs(lab):
    study = _study(lab)
    run = lab.create_run(1, study["id"], name="r", definition={"factor": "mom20", "direction": "high"})
    with users._conn() as c:
        c.execute("UPDATE factor_runs SET status='running', started_at=1 WHERE id=?", (run["id"],))
    assert lab.recover_abandoned() == 1
    recovered = lab.get_run(1, study["id"], run["id"])
    assert recovered["status"] == "failed"
    assert "重启" in recovered["error"]
    assert recovered["definition"] == {"factor": "mom20", "direction": "high"}  # 原始定义保留
    assert lab.recover_abandoned() == 0


# ---------------------------------------------------------------------------
# worker 子进程入口
# ---------------------------------------------------------------------------

def test_worker_run_catches_errors_into_failed(lab, monkeypatch):
    import factor_lab_worker

    study = _study(lab)
    run = lab.create_run(1, study["id"], name="r", definition={"factor": "mom20", "direction": "high"})
    monkeypatch.setattr(factor_lab_worker, "compute", lambda _run: (_ for _ in ()).throw(RuntimeError("面板读取失败")))
    factor_lab_worker.run(users.DB_FILE, run["id"])
    failed = lab.get_run(1, study["id"], run["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "面板读取失败"


def test_worker_run_skips_non_active(lab):
    import factor_lab_worker

    study = _study(lab)
    run = lab.create_run(1, study["id"], name="r", definition={"factor": "mom20", "direction": "high"})
    lab.cancel_run(1, study["id"], run["id"])
    factor_lab_worker.run(users.DB_FILE, run["id"])
    assert lab.get_run(1, study["id"], run["id"])["status"] == "cancelled"


def test_worker_compute_uses_stored_definition_not_mutable_recipe(lab, monkeypatch):
    """执行用实验落库时的定义快照，而不是可能被改动的自定义因子表。"""
    import factor_lab_worker

    calls = {}
    fake = types.SimpleNamespace()
    fake.input_manifest = FakeAnalysis().input_manifest
    fake.execute = lambda defs, config, phase, manifest, cancelled=None: calls.update(
        defs=defs, config=config, phase=phase) or {"native": [{}]}
    fake.backtest = lambda *a, **k: {"metrics": {}}
    monkeypatch.setitem(sys.modules, "factor_analysis", fake)

    study = _study(lab)
    run = lab.create_run(1, study["id"], name="expr",
                         definition={"expr": "cs_rank(close)", "direction": "high"})
    mutable = {"expr": "cs_rank(close)"}
    mutable["expr"] = "totally_different"  # 模拟自定义因子表被改
    factor_lab_worker.compute(lab.get_run(1, study["id"], run["id"]))
    assert calls["defs"] == [{"expr": "cs_rank(close)", "direction": "high"}]
    assert calls["phase"] == "development"


def test_worker_compute_backtest_branch(lab, monkeypatch):
    import factor_lab_worker

    calls = {}
    fake = types.SimpleNamespace()
    fake.input_manifest = FakeAnalysis().input_manifest
    fake.execute = lambda *a, **k: {"native": [{}]}
    fake.backtest = lambda definition, config, phase, params, manifest, cancelled=None: calls.update(
        definition=definition, phase=phase, params=params) or {"metrics": {"n_days": 1}}
    monkeypatch.setitem(sys.modules, "factor_analysis", fake)

    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    bt = lab.create_backtest(1, study["id"], "bt", dev["id"], top_n=30)
    result = factor_lab_worker.compute(lab.get_run(1, study["id"], bt["id"]))
    assert result == {"metrics": {"n_days": 1}}
    assert calls["definition"] == dev["definition"]
    assert calls["params"]["top_n"] == 30


def test_parent_lane_spawns_subprocess_and_records_terminal(lab, tmp_path, monkeypatch):
    """真实父车道：起子进程 → 子进程条件写终态 → 父进程不覆盖成功结果。"""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "lab_stub_worker.py").write_text(
        "import factor_lab, users\n"
        "def run(db_file, run_id):\n"
        "    users.DB_FILE = db_file\n"
        "    factor_lab.init_db()\n"
        "    factor_lab.finish_run(run_id, status='completed', result={'native': [{'stub': True}]})\n"
        "if __name__ == '__main__':\n"
        "    import sys; run(sys.argv[1], sys.argv[2])\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(stub_dir))
    monkeypatch.setattr(factor_lab, "WORKER_MODULE", "lab_stub_worker")
    study = _study(lab)
    run = lab.create_run(1, study["id"], name="r", definition={"factor": "mom20", "direction": "high"})
    factor_lab._execute(run["id"])
    final = lab.get_run(1, study["id"], run["id"])
    assert final["status"] == "completed"
    assert final["result"] == {"native": [{"stub": True}]}
    assert final["started_at"] is not None and final["finished_at"] is not None


def test_parent_lane_cancel_terminates_and_keeps_cancelled(lab, tmp_path, monkeypatch):
    """取消必须终止子进程，且后到的完成结果不得覆盖 cancelled。"""
    import threading
    import time

    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "lab_slow_worker.py").write_text(
        "import time, factor_lab, users\n"
        "def run(db_file, run_id):\n"
        "    users.DB_FILE = db_file\n"
        "    factor_lab.init_db()\n"
        "    time.sleep(5)\n"
        "    factor_lab.finish_run(run_id, status='completed', result={'native': [{}]})\n"
        "if __name__ == '__main__':\n"
        "    import sys; run(sys.argv[1], sys.argv[2])\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(stub_dir))
    monkeypatch.setattr(factor_lab, "WORKER_MODULE", "lab_slow_worker")
    study = _study(lab)
    run = lab.create_run(1, study["id"], name="r", definition={"factor": "mom20", "direction": "high"})
    thread = threading.Thread(target=factor_lab._execute, args=(run["id"],))
    thread.start()
    time.sleep(1.0)
    lab.cancel_run(1, study["id"], run["id"])
    thread.join(timeout=30)
    final = lab.get_run(1, study["id"], run["id"])
    assert final["status"] == "cancelled"
    assert final["result"] is None


# ---------------------------------------------------------------------------
# 真实计算端到端（factor_analysis 就绪后生效）
# ---------------------------------------------------------------------------

def _write_dataset(root: str, n_codes: int = 40, n_days: int = 220) -> None:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(11)
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2023-01-02", periods=n_days)]
    rows = []
    for i in range(n_codes):
        code = f"{600000 + i:06d}"
        base = 10.0 + i * 0.5
        for t, date in enumerate(dates):
            close = base * (1 + 0.0008 * t) * float(np.exp(rng.normal(0, 0.008)))
            rows.append({"code": code, "date": date, "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": 1e6, "amount": close * 1e6})
            base = close
    factor_dir = os.path.join(root, "factor")
    os.makedirs(factor_dir, exist_ok=True)
    panel = pd.DataFrame(rows)
    panel.to_csv(os.path.join(factor_dir, "bars.csv.gz"), index=False, compression="gzip")
    inst = pd.DataFrame({"code": sorted(panel["code"].unique()),
                         "name": ["合成股"] * n_codes,
                         "list_date": [dates[0]] * n_codes})
    inst.to_csv(os.path.join(factor_dir, "instruments.csv.gz"), index=False, compression="gzip")
    with open(os.path.join(factor_dir, "calendar.json"), "w", encoding="utf-8") as f:
        json.dump({"dates": dates, "index_close": {}, "built_at": "2023-12-29 15:00:00"}, f)
    with open(os.path.join(factor_dir, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump({"built_at": "2023-12-29 15:00:00", "codes": n_codes, "dates": n_days}, f)


_DRIVER = r'''
import json, os, sys, time
import users, factor_lab, factor_analysis, factor_data

users.DB_FILE = sys.argv[1]
factor_lab.init_db()
factor_lab.start_worker()
dates = factor_data.load_calendar()
assert len(dates) >= 200, dates[:2]
config = {
    "horizon": 5, "min_days_listed": 1,
    "development": {"start": dates[10], "end": dates[120]},
    "validation": {"start": dates[121], "end": dates[160]},
    "holdout": {"start": dates[161], "end": dates[200]},
}
files_before = sorted(os.listdir(factor_data.DATA_DIR))
study = factor_lab.create_study(1, "端到端", "", config)
sid = study["id"]
def wait(run):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = factor_lab.get_run(1, sid, run["id"])
        if current["status"] in factor_lab.TERMINAL:
            assert current["status"] == "completed", current
            return current
        time.sleep(.05)
    raise AssertionError("worker did not finish")

a = factor_lab.create_run(1, sid, name="dev", definition={"factor": "mom20", "direction": "high"})
b = factor_lab.create_run(1, sid, name="expr", definition={"expr": "cs_rank(close)", "direction": "low"})
factor_lab.submit(a["id"])
factor_lab.submit(b["id"])
a, b = wait(a), wait(b)
assert a["finished_at"] <= b["started_at"], (a, b)
for run in (a, b):
    assert len(run["result"]["native"]) == 1
    stats = run["result"]["native"][0]["by_horizon"]["5"]
    assert stats["sample"]["paired_rows"] > 0
    assert stats["n_days"] > 0 and stats["ic"]["rank_ic_mean"] is not None

compare = factor_lab.create_compare(1, sid, "比较", [a["id"], b["id"]])
factor_lab.submit(compare["id"])
compare = wait(compare)
assert len(compare["result"]["native"]) == 2
assert compare["result"]["common"]["by_horizon"]["5"]["paired_rows"] > 0
assert len(compare["result"]["correlation"]["matrix"]) == 2

bt = factor_lab.create_backtest(1, sid, "回测", b["id"], top_n=10, freq="weekly", cost=.5)
factor_lab.submit(bt["id"])
bt = wait(bt)
assert bt["result"]["definition"] == b["definition"]
assert len(bt["result"]["nav"]["dates"]) >= 40
assert bt["input_manifest"] == b["input_manifest"]

factor_lab.update_study(1, sid, baseline_id=a["id"])
factor_lab.freeze_study(1, sid, [b["id"]])
for phase in ("validation", "holdout"):
    staged = factor_lab.create_run(1, sid, name=phase, phase=phase, source_run_id=b["id"])
    factor_lab.submit(staged["id"])
    staged = wait(staged)
    assert staged["definition"] == b["definition"]
    assert staged["result"]["phase"] == phase
factor_lab.add_decision(1, sid, "保留候选", "合成数据流程验证", [a["id"], b["id"]])
export = factor_lab.export_study(1, sid)
assert len(export["runs"]) == 6 and export["study"]["holdout_used_at"]
assert factor_lab.export_study(2, sid) is None
assert sorted(os.listdir(factor_data.DATA_DIR)) == files_before, "shared artifacts changed"
assert factor_lab.recover_abandoned() == 0
print(json.dumps({"status": a["status"], "error": a["error"], "study_id": sid,
                  "result": a["result"], "manifest": a["input_manifest"]}, ensure_ascii=False))
'''

def test_real_worker_compute_integration(tmp_path):
    """真实子进程 + 真实 factor_analysis + 临时 CSV 数据集。"""
    data_dir = tmp_path / "data"
    _write_dataset(str(data_dir))
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    db_file = str(tmp_path / "users.db")
    env = {**os.environ, "MW_DATA_DIR": str(data_dir), "PYTHONPATH": os.path.dirname(os.path.abspath(factor_lab.__file__))}
    proc = subprocess.run([sys.executable, str(driver), db_file], capture_output=True, text=True,
                          timeout=600, env=env, cwd=os.path.dirname(os.path.abspath(factor_lab.__file__)))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["status"] == "completed", payload
    assert payload["result"]["native"][0]["definition"]["factor"] == "mom20"
    assert payload["manifest"]["id"]
    # The database remains independently readable after the executing process exits.
    import sqlite3
    with sqlite3.connect(db_file) as c:
        stored = c.execute("SELECT status, result FROM factor_runs ORDER BY created_at").fetchall()
    assert len(stored) == 6 and all(status == "completed" for status, _ in stored)
    assert json.loads(stored[2][1])["common"]["by_horizon"]["5"]["paired_rows"] > 0


def test_history_appends_are_atomic_under_concurrent_requests(lab):
    from concurrent.futures import ThreadPoolExecutor

    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    lab.update_study(1, study["id"], baseline_id=dev["id"])
    def append(i):
        lab.freeze_study(1, study["id"], [dev["id"]])
        lab.add_decision(1, study["id"], "暂缓", str(i), [dev["id"]])
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(append, range(12)))
    final = lab.get_study(1, study["id"])
    assert len(final["freezes"]) == len(final["decisions"]) == 12
    assert {d["note"] for d in final["decisions"]} == {str(i) for i in range(12)}
    assert len({f["id"] for f in final["freezes"]}) == 12


def test_freeze_limit_includes_automatically_added_baseline(lab):
    study = _study(lab)
    runs = [_completed_run(lab, 1, study["id"], name=str(i)) for i in range(5)]
    lab.update_study(1, study["id"], baseline_id=runs[0]["id"])
    with pytest.raises(factor_lab.FactorLabError):
        lab.freeze_study(1, study["id"], [r["id"] for r in runs[1:]])
    assert lab.get_study(1, study["id"])["freezes"] == []


def test_invalid_holdout_does_not_consume_marker_or_insert(lab, monkeypatch):
    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    lab.update_study(1, study["id"], baseline_id=dev["id"])
    lab.freeze_study(1, study["id"], [dev["id"]])
    for kwargs in ({"name": " "}, {"name": "h", "definition": dev["definition"]},
                   {"name": "h", "source_run_id": "missing"}):
        request = {"name": "h", "phase": "holdout", "source_run_id": dev["id"], **kwargs}
        with pytest.raises((factor_lab.FactorLabError, LookupError)):
            lab.create_run(1, study["id"], **request)
    monkeypatch.setattr(lab, "MAX_PENDING", 0)
    with pytest.raises(factor_lab.FactorLabConflict):
        lab.create_run(1, study["id"], name="h", phase="holdout", source_run_id=dev["id"])
    assert lab.get_study(1, study["id"])["holdout_used_at"] is None
    assert len(lab.list_runs(1, study["id"])) == 1


def test_backtest_keeps_source_manifest_and_staged_version_checks_are_full(lab, monkeypatch):
    study = _study(lab)
    dev = _completed_run(lab, 1, study["id"])
    lab.update_study(1, study["id"], baseline_id=dev["id"])
    lab.freeze_study(1, study["id"], [dev["id"]])
    changed = FakeAnalysis()
    original = changed.input_manifest()
    changed.input_manifest = lambda: {**original, "data_version": "changed-but-same-id"}
    monkeypatch.setattr(lab, "_analysis", lambda: changed)
    with pytest.raises(factor_lab.FactorLabConflict):
        lab.create_run(1, study["id"], name="v", phase="validation", source_run_id=dev["id"])
    bt = lab.create_backtest(1, study["id"], "bt", dev["id"])
    assert bt["input_manifest"] == dev["input_manifest"]
    new = _completed_run(lab, 1, study["id"], name="new")
    with pytest.raises(factor_lab.FactorLabError):
        lab.create_compare(1, study["id"], "compare", [dev["id"], new["id"]])


def test_completed_result_and_caller_snapshots_are_immutable(lab):
    config = _valid_config()
    study = lab.create_study(1, "study", "", config)
    definition = {"expr": "cs_rank(close)", "direction": "low"}
    run = lab.create_run(1, study["id"], name="r", definition=definition)
    config["development"]["end"] = "9999-12-31"
    definition["expr"] = "mutated"
    run["definition"]["direction"] = "high"
    result = {"native": [{"evidence": 7}]}
    assert lab.finish_run(run["id"], status="completed", result=result)
    result["native"][0]["evidence"] = 0
    assert not lab.finish_run(run["id"], status="failed", error="late failure")
    assert lab.cancel_run(1, study["id"], run["id"])["status"] == "completed"
    stored = lab.get_run(1, study["id"], run["id"])
    assert stored["result"] == {"native": [{"evidence": 7}]}
    assert stored["definition"] == {"expr": "cs_rank(close)", "direction": "low"}
    assert lab.get_study(1, study["id"])["config"] == _valid_config()
    assert users.get_data(1) == {}  # Results never enter the generic page payload.


def test_export_scrubs_nested_result_manifest_paths(lab):
    study = _study(lab)
    run = lab.create_run(1, study["id"], name="r", definition={"factor": "mom20", "direction": "high"})
    manifest = {"id": "m", "files": {"bars": {"path": "/private/secret/bars.csv.gz"},
                                          "catalog": {"path": "C:\\secret\\catalog.json"}}}
    lab.finish_run(run["id"], status="completed", result={"input_manifest": manifest})
    export = lab.export_study(1, study["id"])
    text = json.dumps(export)
    assert "secret" not in text and "/Users/x" not in text
    assert export["runs"][0]["result"]["input_manifest"]["files"]["bars"]["path"] == "bars.csv.gz"
    assert lab.get_run(1, study["id"], run["id"])["result"]["input_manifest"] == manifest


def test_worker_detects_changes_after_computation(lab, monkeypatch):
    import factor_lab_worker

    study = _study(lab)
    run = lab.create_run(1, study["id"], name="r", definition={"factor": "mom20", "direction": "high"})
    fake = types.SimpleNamespace()
    current = [run["input_manifest"]]
    fake.input_manifest = lambda: current[0]
    def execute(*args, **kwargs):
        current[0] = FakeAnalysis("changed-during-execution").input_manifest()
        return {"native": [{"wrong": True}]}
    fake.execute = execute
    monkeypatch.setitem(sys.modules, "factor_analysis", fake)
    factor_lab_worker.run(users.DB_FILE, run["id"])
    final = lab.get_run(1, study["id"], run["id"])
    assert final["status"] == "failed" and final["result"] is None
    assert "输入数据已变化" in final["error"]


def test_import_does_not_open_database_start_jobs_or_import_analysis():
    code = """
import sys, types
users = types.ModuleType('users')
users._conn = lambda: (_ for _ in ()).throw(AssertionError('database opened during import'))
sys.modules['users'] = users
import factor_lab, factor_lab_routes, factor_lab_worker
assert factor_lab._THREAD is None
assert not factor_lab._QUEUE
assert 'factor_analysis' not in sys.modules
"""
    env = {**os.environ, "PYTHONPATH": os.path.dirname(factor_lab.__file__)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr


def _install_worker_probe(tmp_path, monkeypatch, *, sleep=0.0, noisy=False):
    stub = tmp_path / "probe"
    stub.mkdir()
    worker = """
import json, os, sys, time, users, factor_lab
users.DB_FILE = sys.argv[1]
rid = sys.argv[2]
def event(name):
    with open(users.DB_FILE + '.lane-events', 'a', encoding='utf-8') as f:
        f.write(json.dumps([rid, name, time.time(), os.getpid()]) + '\\n')
event('start')
with users._conn() as c:
    active = c.execute("SELECT COUNT(*) FROM factor_runs WHERE status='running'").fetchone()[0]
time.sleep(__SLEEP__)
if __NOISY__:
    sys.stderr.write('x' * 100000)
event('end')
factor_lab.finish_run(rid, status='completed', result={'active': active})
"""
    worker = worker.replace('__SLEEP__', repr(sleep)).replace('__NOISY__', repr(noisy))
    (stub / "lab_probe_worker.py").write_text(worker, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(stub), os.path.dirname(factor_lab.__file__))))
    monkeypatch.setattr(factor_lab, "WORKER_MODULE", "lab_probe_worker")
    return stub

def test_process_lane_serializes_independent_parents_and_duplicate_claims(lab, tmp_path, monkeypatch):
    _install_worker_probe(tmp_path, monkeypatch, sleep=.4)
    study = _study(lab)
    runs = [lab.create_run(1, study["id"], name=str(i), definition={"factor": "mom20", "direction": "high"})
            for i in range(2)]
    code = "import sys, users, factor_lab; users.DB_FILE=sys.argv[1]; factor_lab.WORKER_MODULE='lab_probe_worker'; factor_lab._execute(sys.argv[2])"
    processes = [subprocess.Popen([sys.executable, "-c", code, users.DB_FILE, run["id"]])
                 for run in (runs[0], runs[0], runs[1])]
    try:
        for proc in processes:
            assert proc.wait(timeout=30) == 0
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    stored = sorted([lab.get_run(1, study["id"], r["id"]) for r in runs], key=lambda r: r["started_at"])
    assert all(r["status"] == "completed" and r["result"] == {"active": 1} for r in stored)
    assert stored[0]["finished_at"] <= stored[1]["started_at"]


def test_restart_fails_orphan_without_releasing_its_memory_lane(lab, tmp_path, monkeypatch):
    import time

    _install_worker_probe(tmp_path, monkeypatch, sleep=.8)
    study = _study(lab)
    old_run = lab.create_run(1, study["id"], name="old", definition={"factor": "mom20", "direction": "high"})
    code = "import sys, users, factor_lab; users.DB_FILE=sys.argv[1]; factor_lab.WORKER_MODULE='lab_probe_worker'; factor_lab._execute(sys.argv[2])"
    parent = subprocess.Popen([sys.executable, "-c", code, users.DB_FILE, old_run["id"]])
    events = users.DB_FILE + ".lane-events"
    try:
        deadline = time.monotonic() + 10
        while not os.path.exists(events) and time.monotonic() < deadline:
            time.sleep(.01)
        assert os.path.exists(events), "child did not start"
    finally:
        parent.kill()
        parent.wait(timeout=10)
    assert lab.recover_abandoned() == 1
    new_run = lab.create_run(1, study["id"], name="new", definition={"factor": "mom20", "direction": "high"})
    lab._execute(new_run["id"])
    with open(events, encoding="utf-8") as f:
        trace = [json.loads(line) for line in f]
    assert [(row[0], row[1]) for row in trace] == [
        (old_run["id"], "start"), (old_run["id"], "end"),
        (new_run["id"], "start"), (new_run["id"], "end"),
    ]
    old = lab.get_run(1, study["id"], old_run["id"])
    assert old["status"] == "failed" and "重启" in old["error"] and old["result"] is None
    assert lab.get_run(1, study["id"], new_run["id"])["status"] == "completed"


def test_worker_stderr_cannot_deadlock_and_timeout_is_visible(lab, tmp_path, monkeypatch):
    _install_worker_probe(tmp_path, monkeypatch, noisy=True)
    study = _study(lab)
    run = lab.create_run(1, study["id"], name="noisy", definition={"factor": "mom20", "direction": "high"})
    lab._execute(run["id"])
    assert lab.get_run(1, study["id"], run["id"])["status"] == "completed"
    monkeypatch.setattr(lab, "_RUN_TIMEOUT", 0)
    timeout = lab.create_run(1, study["id"], name="timeout", definition={"factor": "mom20", "direction": "high"})
    lab._execute(timeout["id"])
    final = lab.get_run(1, study["id"], timeout["id"])
    assert final["status"] == "failed" and "超时" in final["error"]


def test_stop_worker_terminates_child_interrupts_pending_and_preserves_terminals(lab, tmp_path, monkeypatch):
    import time

    _install_worker_probe(tmp_path, monkeypatch, sleep=2.0)
    study = _study(lab)
    active = lab.create_run(1, study["id"], name="active", definition={"factor": "mom20", "direction": "high"})
    queued = lab.create_run(1, study["id"], name="queued", definition=active["definition"])
    completed = _completed_run(lab, 1, study["id"], name="done")
    cancelled = lab.create_run(1, study["id"], name="cancel", definition=active["definition"])
    cancelled = lab.cancel_run(1, study["id"], cancelled["id"])
    lab.submit(active["id"])
    lab.submit(queued["id"])
    events = users.DB_FILE + ".lane-events"
    deadline = time.monotonic() + 10
    while (not os.path.exists(events) or os.path.getsize(events) == 0) and time.monotonic() < deadline:
        time.sleep(.01)
    with open(events, encoding="utf-8") as f:
        pid = json.loads(f.readline())[3]
    lab.start_worker()  # Repeated startup must not run recovery against a live job.
    assert lab.get_run(1, study["id"], active["id"])["status"] == "running"
    lab.stop_worker()
    assert lab._THREAD is None and not lab._QUEUE
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # The calculation child was terminated and reaped, not orphaned.
    for run in (active, queued):
        final = lab.get_run(1, study["id"], run["id"])
        assert final["status"] == "failed" and final["error"] == "服务停止，任务已中断"
        assert final["result"] is None and final["definition"] == run["definition"]
        assert not lab.finish_run(run["id"], status="completed", result={"late": True})
    assert lab.get_run(1, study["id"], queued["id"])["started_at"] is None
    assert lab.get_run(1, study["id"], completed["id"]) == completed
    assert lab.get_run(1, study["id"], cancelled["id"]) == cancelled
    late = lab.create_run(1, study["id"], name="late", definition=active["definition"])
    with pytest.raises(factor_lab.FactorLabConflict):
        lab.submit(late["id"])
    assert lab.get_run(1, study["id"], late["id"])["status"] == "failed"
    lab.stop_worker()  # Idempotent cleanup; another lifecycle may start normally.
    lab.start_worker()
    retry = lab.create_run(1, study["id"], name="retry", definition=active["definition"])
    lab.submit(retry["id"])
    deadline = time.monotonic() + 10
    while lab.get_run(1, study["id"], retry["id"])["status"] in lab.ACTIVE and time.monotonic() < deadline:
        time.sleep(.02)
    assert lab.get_run(1, study["id"], retry["id"])["status"] == "completed"


def test_stop_worker_does_not_hang_waiting_for_another_process_lane(lab):
    import fcntl
    import time

    study = _study(lab)
    run = lab.create_run(1, study["id"], name="waiting", definition={"factor": "mom20", "direction": "high"})
    lane_path = os.path.join(os.path.dirname(users.DB_FILE), "factor-lab-worker.lock")
    with open(lane_path, "a+") as lane:
        fcntl.flock(lane, fcntl.LOCK_EX)
        lab.submit(run["id"])
        time.sleep(.05)
        before = time.monotonic()
        lab.stop_worker()
        assert time.monotonic() - before < 2
    assert lab._THREAD is None
    assert lab.get_run(1, study["id"], run["id"])["status"] == "failed"


def test_missing_dataset_study_is_allowed_but_execution_fails(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = """
import json, sys, users, factor_lab
users.DB_FILE=sys.argv[1]
factor_lab.init_db()
study=factor_lab.create_study(1, '未构建数据', '', json.loads(sys.argv[2]))
run=factor_lab.create_run(1, study['id'], name='dev', definition={'factor':'mom20', 'direction':'high'})
factor_lab._execute(run['id'])
final=factor_lab.get_run(1, study['id'], run['id'])
assert final['status']=='failed' and final['result'] is None, final
assert final['definition']==run['definition']
assert final['error'] and len(final['error']) <= 80
"""
    env = {**os.environ, "MW_DATA_DIR": str(empty), "PYTHONPATH": os.path.dirname(factor_lab.__file__)}
    proc = subprocess.run([sys.executable, "-c", code, str(tmp_path / "empty.db"), json.dumps(_valid_config())],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
