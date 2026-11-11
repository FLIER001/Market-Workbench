"""因子实验计算子进程入口。

调用方式（无 shell/代码拼接）：`python -m factor_lab_worker DB_FILE RUN_ID`
只接受可信的 DB 路径与 run id；通过条件终态写保证「已取消的任务不会被后到的完成结果覆盖」。
任何异常都在这里收口为 failed 状态，不让车道留下永久 running 记录。
"""
from __future__ import annotations

import sys
import traceback

import factor_lab


def run(db_file: str, run_id: str) -> None:
    import users

    users.DB_FILE = db_file  # 可信入参：父进程传入的同一 users.db
    factor_lab.init_db()
    run = factor_lab.get_run_by_id(run_id)
    if run is None or run["status"] not in factor_lab.ACTIVE:
        return
    try:
        result = compute(run)
        factor_lab.finish_run(run_id, status="completed", result=result)
    except Exception as exc:  # noqa: BLE001 - 任何失败都要落成可见的 failed 记录
        traceback.print_exc(file=sys.stderr)
        status = "cancelled" if factor_lab.is_cancelled(run_id) else "failed"
        factor_lab.finish_run(run_id, status=status, error=None if status == "cancelled" else _short_error(exc))


def compute(run: dict) -> dict:
    import factor_analysis

    cancelled = lambda: factor_lab.is_cancelled(run["id"])
    study = factor_lab.get_study_by_id(run["study_id"])
    if study is None:
        raise RuntimeError("研究不存在")
    config = study["config"]
    phase = run["phase"]
    if factor_analysis.input_manifest() != run["input_manifest"]:
        raise ValueError("输入数据已变化，请新建实验")
    if run["kind"] == "backtest":
        result = factor_analysis.backtest(
            run["definition"], config, phase, run["params"], run["input_manifest"], cancelled=cancelled
        )
    elif run["kind"] == "compare":
        source = [factor_lab.get_run_by_id(rid) for rid in run["source_run_ids"]]
        definitions = [s["definition"] for s in source if s and s["study_id"] == run["study_id"]
                       and s["kind"] == "evaluate" and s["status"] == "completed"
                       and s["phase"] == phase and s["input_manifest"] == run["input_manifest"]]
        if len(definitions) != len(run["source_run_ids"]):
            raise RuntimeError("对比的来源实验不存在")
        result = factor_analysis.execute(definitions, config, phase, run["input_manifest"],
                                         cancelled=cancelled)
    elif run["kind"] == "evaluate":
        result = factor_analysis.execute([run["definition"]], config, phase, run["input_manifest"],
                                         cancelled=cancelled)
    else:
        raise ValueError("不支持的任务类型")
    if cancelled():
        raise RuntimeError("任务已取消")
    if factor_analysis.input_manifest() != run["input_manifest"]:
        raise ValueError("输入数据已变化，请新建实验")
    return result


def _short_error(exc: Exception) -> str:
    text = str(exc).strip()
    # Tracebacks/local paths stay in server logs, not in user-visible records or exports.
    if not text or not any("\u4e00" <= ch <= "\u9fff" for ch in text) or "/" in text or "\\" in text:
        return "计算失败，请检查数据和参数"
    return text[:80]


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
