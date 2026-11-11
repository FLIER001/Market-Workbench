"""因子研究执行层：纯函数，被持久化任务与测试共用（见 docs/research/factor-lab-api.md）。

职责边界：
- 输入指纹（日线/股票池/日历/财务 + 计算代码），执行前后一致性校验；
- 定义/配置校验（内置或沙箱公式；主周期 + 上市过滤 + 三段有序不重叠期间）；
- execute：一次加载面板，保留历史预热，按阶段截断前瞻标签，输出各 1/5/10/20 周期
  的 native 统计与共同样本统计（多因子时），以及因子值日度截面秩相关；
- backtest：复用现有组合模拟器，快照因子值，不写共享工件。

口径要点：
- 方向（high/low）在统计前**只应用一次**，统计与组合排序共用同一列；
- 标签 end 交易日 > 阶段结束日的样本置空，防止开发期标签进入验证/留出期；
- 缺失/同值/常数因子返回 null 证据并计入审计，不伪造 0 分；
- 仅一次全市场面板加载；取消在单元之间检查，取消绝不返回成功证据。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date as calendar_date

import numpy as np
import pandas as pd

import factor_backtest
import factor_data
import factor_expr
import factor_pit
import factors

HORIZONS: tuple[int, ...] = (1, 5, 10, 20)
PHASES = ("development", "validation", "holdout")
MAX_DEFINITIONS = 4  # 契约：一次比较 2-4 个因子
MIN_CROSS_SECTION = factors.MIN_CROSS_SECTION
MIN_CORR_ROWS = 3  # 秩相关至少需要的当日联合样本（低于此无法定义相关）

# 计算代码指纹清单（相对 backend 目录的逻辑标签；导出元数据里只出现标签，不含绝对路径）
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_CODE_FILES = (
    "factor_analysis.py", "factors.py", "factor_expr.py",
    "factor_backtest.py", "factor_data.py", "factor_pit.py",
)

TIMING_NOTE = (
    "T 日收盘因子值；统计标签为 T→T+h 收盘收益（诊断口径，非可成交收益）；"
    "组合回测按 T 日信号 T+1 开盘成交。多日前瞻标签重叠，IC IR 不年化。"
)

# 大文件按 stat 缓存流式哈希：文件未变就不重复读盘，也不整文件载入内存。
_HASH_CACHE: dict[str, tuple[tuple[int, ...], dict]] = {}


class CancelledError(RuntimeError):
    """执行被取消：必须抛出，绝不返回成功证据。"""


class InputChangedError(RuntimeError):
    """输入指纹在执行前后不一致（数据被重建或代码被修改）。"""


def _check(cancelled) -> None:
    if cancelled is not None and cancelled():
        raise CancelledError("执行已取消")


# ---------------------------------------------------------------------------
# 输入指纹
# ---------------------------------------------------------------------------

def _fingerprint(path: str) -> dict:
    """文件指纹：存在性 + 字节数 + 内容 sha256（前 16 位）。大文件流式分块哈希。"""
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        _HASH_CACHE.pop(path, None)
        return {"present": False}
    key = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
    cached = _HASH_CACHE.get(path)
    if cached is not None and cached[0] == key:
        return dict(cached[1])
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        after = os.stat(path)
    except FileNotFoundError as exc:
        raise InputChangedError("输入文件在读取时变化") from exc
    if key != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
        raise InputChangedError("输入文件在读取时变化")
    info = {"present": True, "bytes": int(key[0]), "sha256": digest.hexdigest()[:16]}
    _HASH_CACHE[path] = (key, info)
    return dict(info)


def input_manifest() -> dict:
    """输入文件指纹 + 计算代码指纹。

    files 键为逻辑标签（bars/instruments/calendar/catalog/fundamentals），
    值为 {present, bytes, sha256}；不写入本地绝对路径。
    """
    files = {
        "bars": _fingerprint(factor_data._BARS_FILE),
        "instruments": _fingerprint(factor_data._INSTRUMENTS_FILE),
        "calendar": _fingerprint(factor_data._CALENDAR_FILE),
        "catalog": _fingerprint(factor_data._CATALOG_FILE),
        "fundamentals": _fingerprint(factor_pit._FUND_FILE),
    }
    code = {name: _fingerprint(os.path.join(_BACKEND_DIR, name)).get("sha256", "")
            for name in _CODE_FILES}
    version = str(factor_data.data_version() or "")
    return {"id": _manifest_id(version, files, code), "data_version": version,
            "files": files, "code": code}


def _manifest_id(version: str, files: dict, code: dict) -> str:
    blob = json.dumps({"data_version": version, "files": files, "code": code},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _assert_manifest(expected: dict | None, current: dict) -> None:
    """expected 非空时，要求当前输入指纹与之一致（版本/数据/代码任一变化即失败）。"""
    if expected is None:
        return
    if not isinstance(expected, dict) or expected != current:
        raise InputChangedError("输入数据或计算代码已变化，请重新创建实验")


def _require_inputs(manifest: dict) -> None:
    if not manifest["data_version"] or any(
        not manifest["files"][name].get("present")
        for name in ("bars", "catalog", "instruments", "calendar")
    ):
        raise ValueError("因子输入数据未构建完整")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _iso_date(value) -> str:
    if not isinstance(value, str):
        raise ValueError("日期需为 YYYY-MM-DD 字符串")
    try:
        valid = calendar_date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("日期需为 YYYY-MM-DD 格式的有效日期")
    return value


def validate_definition(definition: dict) -> dict:
    """校验内置因子或沙箱公式，返回可序列化的规范化定义。"""
    if not isinstance(definition, dict):
        raise ValueError("因子定义必须是对象")
    factor = definition.get("factor")
    expr = definition.get("expr")
    direction = definition.get("direction")
    has_factor = "factor" in definition
    has_expr = "expr" in definition
    if has_factor == has_expr:
        raise ValueError("因子定义必须且只能包含 factor 或 expr 之一")
    if direction not in ("high", "low"):
        raise ValueError("方向必须是 high 或 low")
    if has_factor:
        if not isinstance(factor, str) or not factor.strip():
            raise ValueError("因子名必须是非空字符串")
        name = factor.strip()
        if name.startswith("custom:"):
            saved = factor_expr.lookup_custom(name.split(":", 1)[1])
            if saved is None:
                raise ValueError(f"自定义因子 {name} 不存在")
            # 研究保存公式快照，后续修改或删除遗留目录不能改变已冻结定义。
            expr = saved.get("expr")
        else:
            if name not in factors.FACTOR_META and name not in factors.FIN_FACTOR_META:
                raise ValueError(f"未知因子 {name}")
            return {"factor": name, "direction": direction}
    if not isinstance(expr, str):
        raise ValueError("公式必须是字符串")
    expr = expr.strip()
    ast = factor_expr.compile_expr(expr)
    if not factor_expr.referenced_fields(ast):
        raise ValueError("公式结果不含任何字段（全是常数）")
    return {"expr": expr, "direction": direction}


def validate_config(config: dict) -> dict:
    """规范化主周期、上市过滤与有序不重叠的三段期间。不发明股票池、不用未来算子。"""
    if not isinstance(config, dict):
        raise ValueError("研究配置必须是对象")
    if set(config) - {"horizon", "min_days_listed", *PHASES}:
        raise ValueError("研究配置包含不支持的字段")
    horizon = config.get("horizon")
    if isinstance(horizon, bool) or not isinstance(horizon, (int, float)) or horizon not in HORIZONS:
        raise ValueError("主检验周期必须是 1/5/10/20 之一")
    min_days = config.get("min_days_listed", 60)
    if isinstance(min_days, float) and min_days.is_integer():
        min_days = int(min_days)
    if isinstance(min_days, bool) or not isinstance(min_days, int):
        raise ValueError("上市天数下限必须是整数")
    if not 0 <= min_days <= 5000:
        raise ValueError("上市天数下限需在 0-5000 之间")

    periods: dict[str, dict] = {}
    previous_end: str | None = None
    for phase in PHASES:
        period = config.get(phase)
        if not isinstance(period, dict):
            raise ValueError(f"{phase} 期配置缺失")
        start = _iso_date(period.get("start"))
        end = _iso_date(period.get("end"))
        if start > end:
            raise ValueError(f"{phase} 期开始日期晚于结束日期")
        if previous_end is not None and start <= previous_end:
            raise ValueError("开发/验证/留出期必须按顺序且不重叠")
        previous_end = end
        periods[phase] = {"start": start, "end": end}
    return {"horizon": int(horizon), "min_days_listed": min_days, **periods}


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _finite(value) -> float | None:
    """JSON 安全浮点：非有限值一律 null（禁止把 NaN/Inf 写进结果）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _prepare(frame: pd.DataFrame, column: str, config: dict, start: str, end: str,
             all_dates: list[str], horizons: tuple[int, ...]) -> dict:
    """截取阶段信号区间（保留预热与区间后标签）→ 上市过滤 → 因子列清洗 → 截断越界标签。"""
    dates = frame.index.get_level_values("date")
    window = pd.DataFrame(frame.loc[(dates >= start) & (dates <= end)])
    input_rows = int(len(window))
    input_days = int(window.index.get_level_values("date").nunique())
    window = pd.DataFrame(window.loc[window["days_since_list"] >= config["min_days_listed"]])
    eligible_rows = int(len(window))
    window[column] = pd.to_numeric(window[column], errors="coerce")
    window = window.replace([np.inf, -np.inf], np.nan)
    _mask_future_labels(window, all_dates, end, horizons)
    return {"window": window, "input_rows": input_rows, "input_days": input_days,
            "eligible_rows": eligible_rows}


def _mask_future_labels(window: pd.DataFrame, all_dates: list[str], end: str,
                        horizons: tuple[int, ...]) -> None:
    """把标签结束交易日超出阶段结束日的行置空：开发期标签不得流入验证/留出期。

    标签按统一交易日历 shift，因此信号日 T 的 h 期标签结束日 = 日历上 T 之后第 h 个交易日；
    只要该日 > 阶段结束日，就置 NaN（就地修改 window 副本）。
    """
    position = {date: index for index, date in enumerate(all_dates)}
    last_in_phase = sum(d <= end for d in all_dates) - 1
    dates = window.index.get_level_values("date")
    signal_positions = np.array([position[d] for d in dates], dtype=int)
    for horizon in horizons:
        window.loc[signal_positions + horizon > last_in_phase, f"ret_fwd_{horizon}"] = np.nan


def _long_short_bp(window: pd.DataFrame, column: str, horizon: int, cancelled=None) -> float | None:
    """Q5-Q1 组合的日均价差（bp）；无法形成五分位/无有效标签的日期跳过。"""
    ret_col = f"ret_fwd_{horizon}"
    spreads: list[float] = []
    for _date, day in window.groupby(level="date"):
        _check(cancelled)
        if len(day.dropna(subset=[ret_col])) < MIN_CROSS_SECTION:
            continue
        bins = factors._quantile_bins(pd.Series(day[column]))
        if bins is None:
            continue
        top = day.loc[(bins == 5).to_numpy(), ret_col].mean()
        bottom = day.loc[(bins == 1).to_numpy(), ret_col].mean()
        if top == top and bottom == bottom:  # 非 NaN
            spreads.append(float(top - bottom) * 1e4)
    return _finite(np.mean(spreads)) if spreads else None


def _horizon_stats(window: pd.DataFrame, column: str, horizon: int,
                   audit: dict, cancelled=None) -> dict:
    """单定义单周期的 native 统计。无有效证据一律 null，并给出样本审计。"""
    ret_col = f"ret_fwd_{horizon}"
    input_rows, input_days, eligible_rows = audit["input_rows"], audit["input_days"], audit["eligible_rows"]
    valid = window.dropna(subset=[column])  # 与 legacy 一致：先剔因子缺失，再要求成对样本
    factor_rows = int(len(valid))
    total_days = input_days
    ic_values: dict[str, float] = {}
    rank_ic_values: dict[str, float] = {}
    used_rows = 0
    n_days = 0

    for date, day in valid.groupby(level="date"):
        _check(cancelled)
        if len(day) < MIN_CROSS_SECTION:
            continue
        paired = day.dropna(subset=[ret_col])
        if len(paired) < MIN_CROSS_SECTION:
            continue
        ic = factors._safe_pearson(paired[column].to_numpy(), paired[ret_col].to_numpy())
        rank_ic = factors._safe_spearman(paired[column].to_numpy(), paired[ret_col].to_numpy())
        if ic is None or rank_ic is None:
            continue  # 常数/全同值因子：不记录，避免 null 进序列
        ic_values[str(date)] = ic
        rank_ic_values[str(date)] = rank_ic
        used_rows += int(len(paired))
        n_days += 1

    ic_series = pd.Series(ic_values, dtype=float).sort_index()
    rank_series = pd.Series(rank_ic_values, dtype=float).sort_index()

    paired = valid.dropna(subset=[ret_col])
    quantile_returns, quantile_turnover, rank_autocorr = factors._quantile_stats(
        paired, column, horizon, check=lambda: _check(cancelled))
    long_short = _long_short_bp(paired, column, horizon, cancelled)

    paired_rows = int(len(paired))
    rank_std = float(np.std(rank_series.to_numpy(), ddof=1)) if len(rank_series) > 1 else 0.0

    by_year = factors._ic_by_year(rank_series)

    return {
        "n_days": n_days,
        "avg_coverage": float(used_rows / n_days) if n_days else 0.0,
        "sample": {
            "input_rows": input_rows, "eligible_rows": eligible_rows,
            "factor_rows": factor_rows, "paired_rows": paired_rows, "used_rows": used_rows,
            "loss_ratio": (1.0 - used_rows / input_rows) if input_rows else None,
            "skipped_days": int(total_days - n_days),
        },
        "ic": {
            "ic_mean": _finite(ic_series.mean()) if len(ic_series) else None,
            "rank_ic_mean": _finite(rank_series.mean()) if len(rank_series) else None,
            "rank_ic_ir": _finite(rank_series.mean() / rank_std) if len(rank_series) > 1 and rank_std > 0 else None,
            "rank_ic_positive_ratio": _finite((rank_series > 0).mean()) if len(rank_series) else None,
        },
        "ic_series": {
            "dates": [str(d) for d in rank_series.index],
            "values": [round(float(v), 4) for v in rank_series.to_numpy()],
        },
        "quantile_returns": {k: _finite(v) for k, v in quantile_returns.items()},
        "quantile_turnover": {k: _finite(v) for k, v in quantile_turnover.items()},
        "long_short_bp": long_short,
        "rank_autocorr": _finite(rank_autocorr),
        "by_year": by_year,
    }


def _common_sample(prepared: list[dict], horizons: list[int], cancelled=None) -> dict:
    """共同样本统计：在每个周期上取所有定义因子值与标签都有效的联合行，再各自重算。"""
    by_horizon: dict[str, dict] = {}
    for horizon in horizons:
        ret_col = f"ret_fwd_{horizon}"
        parts: list[pd.DataFrame] = []
        for index, item in enumerate(prepared):
            sub = item["window"][[item["column"], ret_col]].dropna()
            sub.columns = [f"f{index}", f"r{index}"]
            parts.append(sub)
        joint = pd.concat(parts, axis=1, join="inner").dropna()
        days = int(joint.index.get_level_values("date").nunique())
        audit = {"input_rows": len(joint), "input_days": days, "eligible_rows": len(joint)}
        results = []
        for index, item in enumerate(prepared):
            _check(cancelled)
            stats_frame = pd.DataFrame(joint[[f"f{index}", f"r{index}"]])
            stats_frame.columns = [item["column"], ret_col]
            results.append(_horizon_stats(stats_frame, item["column"], horizon, audit, cancelled))
        by_horizon[str(horizon)] = {"results": results, "paired_rows": int(len(joint))}
    return {"by_horizon": by_horizon}


def _correlation(prepared: list[dict], cancelled=None) -> dict:
    """因子值的日度截面秩相关（联合 因子×股票×日期 样本），不是 IC 序列相关。"""
    count = len(prepared)
    series = [item["window"][item["column"]].dropna().rename(f"f{index}")
              for index, item in enumerate(prepared)]
    joint = pd.concat(series, axis=1, join="inner").dropna()
    sums = np.zeros((count, count))
    counts = np.zeros((count, count))
    n_days = 0
    for _date, day in joint.groupby(level="date"):
        _check(cancelled)
        if len(day) < MIN_CORR_ROWS:
            continue
        ranks = day.rank()
        available = False
        for i in range(count):
            for j in range(count):
                value = factors._safe_pearson(ranks.iloc[:, i], ranks.iloc[:, j])
                if value is not None:
                    sums[i, j] += value
                    counts[i, j] += 1
                    available = True
        n_days += int(available)
    matrix = [[_finite(sums[i, j] / counts[i, j]) if counts[i, j] else None for j in range(count)]
              for i in range(count)]
    return {"matrix": matrix, "n_days": n_days, "sample_rows": int(len(joint))}


def _display_name(definition: dict) -> str:
    if definition.get("factor"):
        return factors.factor_name_of(definition["factor"])
    return "自定义公式"


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def execute(definitions: list[dict], config: dict, phase: str, expected_manifest: dict | None,
            cancelled=None) -> dict:
    """一次加载面板，计算各定义的 native 与（多定义时）共同样本统计和因子值秩相关。"""
    _check(cancelled)
    if phase not in PHASES:
        raise ValueError("研究阶段必须是 development/validation/holdout")
    if not isinstance(definitions, list) or not definitions:
        raise ValueError("至少需要一个因子定义")
    if len(definitions) > MAX_DEFINITIONS:
        raise ValueError(f"一次最多比较 {MAX_DEFINITIONS} 个因子")
    normalized = [validate_definition(definition) for definition in definitions]
    cfg = validate_config(config)
    start, end = cfg[phase]["start"], cfg[phase]["end"]

    before = input_manifest()
    _assert_manifest(expected_manifest, before)
    _require_inputs(before)
    _check(cancelled)

    panel = factor_data.load_panel()  # 全流程唯一一次全市场面板加载
    _check(cancelled)
    prepared: list[dict] = []
    if panel.empty:
        raise ValueError("日线数据为空")
    all_dates = sorted(set(factor_data.load_calendar()) | set(panel["date"]))
    for definition in normalized:
        column = factors.factor_column(definition)
        frame = factors.factor_frame(panel, definition)
        sign = 1.0 if definition["direction"] == "high" else -1.0  # 方向只应用一次
        frame[column] = frame[column] * sign
        prepared.append({"definition": definition, "column": column,
                         **_prepare(frame, column, cfg, start, end, all_dates, HORIZONS)})
        del frame
        _check(cancelled)
    del panel

    after = input_manifest()
    _assert_manifest(before, after)
    _check(cancelled)

    horizons = list(HORIZONS)
    native = []
    for item in prepared:
        audit = {key: item[key] for key in ("input_rows", "input_days", "eligible_rows")}
        by_horizon = {str(h): _horizon_stats(item["window"], item["column"], h, audit, cancelled)
                      for h in horizons}
        native.append({"definition": item["definition"], "by_horizon": by_horizon})

    common = None
    correlation = None
    if len(normalized) > 1:
        common = _common_sample(prepared, horizons, cancelled)
        correlation = _correlation(prepared, cancelled)

    _assert_manifest(before, input_manifest())
    _check(cancelled)
    return {
        "phase": phase, "start": start, "end": end,
        "input_manifest": before,
        "horizons": horizons,
        "native": native,
        "common": common,
        "correlation": correlation,
        "biases": list(factor_data.BIAS_LABELS) + [
            "finance_data: 财务字段如使用，为业绩报表披露口径而非完整三表；历史更正可能改变最新公告日期",
        ],
        "timing_note": TIMING_NOTE,
    }


def backtest(definition: dict, config: dict, phase: str, params: dict | None,
             expected_manifest: dict | None, cancelled=None) -> dict:
    """快照组合回测：复用现有模拟器，使用快照因子值，不写共享工件目录。"""
    _check(cancelled)
    if phase not in PHASES:
        raise ValueError("研究阶段必须是 development/validation/holdout")
    normalized = validate_definition(definition)
    cfg = validate_config(config)
    start, end = cfg[phase]["start"], cfg[phase]["end"]

    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("回测参数必须是对象")
    top_n = params.get("top_n", 50)
    top_pct = params.get("top_pct")
    freq = str(params.get("freq", "monthly"))
    cost = params.get("cost", 1.0)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 500:
        raise ValueError("持仓数需为 1-500 的整数")
    if top_pct is not None and not (isinstance(top_pct, (int, float)) and not isinstance(top_pct, bool)
                                    and np.isfinite(top_pct) and 0 < top_pct <= 1):
        raise ValueError("持仓比例需在 (0,1] 之间")
    if freq not in ("weekly", "monthly"):
        raise ValueError("调仓频率必须是 weekly/monthly")
    if not (isinstance(cost, (int, float)) and not isinstance(cost, bool)
            and np.isfinite(cost) and 0 <= cost <= 3):
        raise ValueError("成本倍数需为 0-3")

    before = input_manifest()
    _assert_manifest(expected_manifest, before)
    _require_inputs(before)
    _check(cancelled)

    panel = factor_data.load_panel()
    _check(cancelled)
    if panel.empty:
        raise ValueError("日线数据为空")
    column = factors.factor_column(normalized)
    frame = factors.factor_frame(panel, normalized)
    frame[column] = frame[column] * (1.0 if normalized["direction"] == "high" else -1.0)

    after = input_manifest()
    _assert_manifest(before, after)
    _check(cancelled)

    result = factor_backtest.run_backtest(
        column, start=start, end=end, top_n=top_n, top_pct=top_pct, freq=freq, cost=float(cost),
        min_days_listed=cfg["min_days_listed"], frame=frame, panel=panel, save=False,
        check=lambda: _check(cancelled))
    _assert_manifest(before, input_manifest())
    _check(cancelled)
    result["definition"] = normalized
    result["factor_name"] = _display_name(normalized)
    result["phase"] = phase
    result["input_manifest"] = before
    result["timing_note"] = TIMING_NOTE
    result["biases"] = list(result["biases"]) + [
        "finance_data: 财务字段如使用，为业绩报表披露口径而非完整三表；历史更正可能改变最新公告日期",
    ]
    return result
