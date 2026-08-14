"""宏观模块 → A股未来收益 回测脚本。

方法：
  1. 载入线上 macro_snapshot.json 的指标 hist（与生产同数据）。
  2. 用 market.py 生产引擎（_indicators_as_of + _module_scores + _climate_module_score）
     逐月回放 8 个模块得分（无未来数据：只截取 ≤ 当月的 hist）。
  3. 对齐腾讯指数月K（万得全A代理 sh000985 / 沪深300 sh000300），计算未来 1/3/6 月收益。
  4. 统计每个模块分（及模块分变化 ΔMoM）与未来收益的秩相关 IC（Spearman）、
     分组收益差（前1/3 - 后1/3）、胜率与显著性 t 值。
  5. 再对每个底层宏观指标的方向调整分位做同样检验，找出真正有预测力的单项。

运行：cd backend && ./.venv/bin/python ../research/output/macro_backtest/backtest.py
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

BACKEND = "/Users/k/Vibe-Research/backend"
OUT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND)

# 回测脚本不该写真实数据目录
os.environ["VR_DATA_DIR"] = os.path.join(OUT, "datadir")
os.makedirs(os.environ["VR_DATA_DIR"], exist_ok=True)

import market  # noqa: E402

SNAP = json.load(open(os.path.expanduser("~/.vibe-research/macro_snapshot.json"), encoding="utf-8"))
IND = SNAP["cn"]

BENCH = {
    "wind_all": "sh000985",   # 中证全A：全市场市值加权
    "hs300": "sh000300",      # 沪深300
    "sse": "sh000001",        # 上证指数
}


def load_monthly_close(sym: str) -> dict[str, float]:
    """CSV 日K → 月末收盘 {YYYY-MM: close}。"""
    closes: dict[str, float] = {}
    with open(os.path.join(OUT, f"{sym}.csv")) as f:
        for row in csv.DictReader(f):
            date = row["date"][:7]
            try:
                closes[date] = float(row["close"])
            except (TypeError, ValueError):
                continue
    return dict(sorted(closes.items()))


def fwd_returns(monthly: dict[str, float]) -> dict[str, dict[int, float]]:
    """未来 k 月收益（%），按观察月末计。"""
    keys = list(monthly)
    out: dict[str, dict[int, float]] = {}
    for i, m in enumerate(keys):
        out[m] = {}
        for k in (1, 3, 6):
            j = i + k
            if j < len(keys):
                out[m][k] = (monthly[keys[j]] / monthly[m] - 1.0) * 100.0
    return out


def rank(xs: list[float]) -> list[float]:
    """平均秩（处理并列）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 5:
        return float("nan")
    ra, rb = rank(a), rank(b)
    mean_a = sum(ra) / n
    mean_b = sum(rb) / n
    cov = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in ra)
    var_b = sum((x - mean_b) ** 2 for x in rb)
    if var_a <= 0 or var_b <= 0:
        return float("nan")
    return cov / math.sqrt(var_a * var_b)


def t_stat(ic_series: list[float]) -> float:
    n = len(ic_series)
    if n < 3:
        return float("nan")
    mean = sum(ic_series) / n
    var = sum((x - mean) ** 2 for x in ic_series) / (n - 1)
    if var <= 0:
        return float("nan")
    return mean / math.sqrt(var / n)


def group_spread(scores: list[float], rets: list[float]) -> tuple[float, float]:
    """前1/3均值收益 - 后1/3均值收益，及两组胜率差。"""
    n = len(scores)
    if n < 9:
        return float("nan"), float("nan")
    order = sorted(range(n), key=lambda i: scores[i])
    third = max(n // 3, 1)
    top = [rets[i] for i in order[-third:]]
    bot = [rets[i] for i in order[:third]]
    spread = sum(top) / len(top) - sum(bot) / len(bot)
    win_top = sum(1 for r in top if r > 0) / len(top)
    win_bot = sum(1 for r in bot if r > 0) / len(bot)
    return spread, win_top - win_bot


# ---------------------------------------------------------------------------
# 1. 模块得分逐月回放（生产引擎）
# ---------------------------------------------------------------------------
print("== 回放模块得分（生产引擎，无未来数据） ==")
months = sorted({str(p.get("date", ""))[:7]
                 for card in IND.values() if isinstance(card, dict)
                 for p in card.get("hist") or []
                 if len(str(p.get("date", ""))) >= 7})
# 指标大多始于 2021-08；两融/ERP 等市场层 2026-03 才有。回放窗口取 2021-12 起（留 4 个月热身）
months = [m for m in months if m >= "2021-12"]

module_scores: dict[str, dict[str, float]] = defaultdict(dict)  # name -> {month: score}
for m in months:
    ind_asof = market._indicators_as_of(IND, m)
    mods = market._module_scores(ind_asof, as_of=m)
    for mod in mods:
        if mod.get("score") is not None:
            module_scores[mod["name"]][m] = mod["score"]

# 单指标方向调整分位（供单项检验）
def indicator_pct_series(key: str, direction: str) -> dict[str, float]:
    """逐月：只用 ≤t 的 hist 计算方向调整分位（与生产 _module_scores 相同规则）。"""
    card = IND.get(key)
    if not card:
        return {}
    hist = card.get("hist") or []
    out: dict[str, float] = {}
    for i in range(4, len(hist)):  # 与生产一致：≥4 点才有分位
        vals = [p["v"] for p in hist[:i + 1] if isinstance(p.get("v"), (int, float))]
        if len(vals) < 4:
            continue
        pct = market._pct_rank(vals, vals[-1])
        if direction == "down":
            pct = 100.0 - pct
        out[str(hist[i]["date"])[:7]] = pct
    return out


# 单指标映射（模块定义里的全部计分项，direction 来自生产定义）
ind_directions: dict[str, str] = {}
for name, _icon, _desc, specs in market._MACRO_MODULES:
    if name == market._CLIMATE_MODULE_NAME:
        for _sub, _w, sub_specs in market._MACRO_CLIMATE:
            for key, d, w, _st in sub_specs:
                if w > 0:
                    ind_directions[key] = d
    else:
        for key, d, w in specs:
            if w > 0:
                ind_directions[key] = d

ind_pct: dict[str, dict[str, float]] = {}
for key, d in ind_directions.items():
    series = indicator_pct_series(key, d)
    if series:
        ind_pct[key] = series

# ---------------------------------------------------------------------------
# 2. 对齐基准收益并检验
# ---------------------------------------------------------------------------
results_modules: list[dict] = []
results_inds: list[dict] = []

for bench_name, sym in BENCH.items():
    monthly = load_monthly_close(sym)
    monthly = {m: v for m, v in monthly.items() if m >= "2021-12"}
    fwd = fwd_returns(monthly)
    ret_months = [m for m in monthly if m < "2026-08"]  # 最近月无未来收益

    def evaluate(series: dict[str, float], label: str, is_module: bool):
        pairs = [(series[m], fwd[m]) for m in ret_months if m in series and k in fwd.get(m, {})]
        if len(pairs) < 12:
            return
        scores = [p[0] for p in pairs]
        rets_k = [p[1][k] for p in pairs]
        ic = spearman(scores, rets_k)
        spread, win_diff = group_spread(scores, rets_k)
        hit = sum(1 for s, r in zip(scores, rets_k)
                  if (s > 50) == (r > 0)) / len(pairs)
        rec = {"bench": bench_name, "label": label, "horizon": k, "n": len(pairs),
               "ic": round(ic, 3), "t": round(ic * math.sqrt(len(pairs) - 2) /
                                              math.sqrt(1 - ic * ic), 2) if abs(ic) < 1 else float("nan"),
               "spread": round(spread, 2), "win_diff": round(win_diff, 2), "hit": round(hit, 3),
               "type": "module" if is_module else "indicator"}
        (results_modules if is_module else results_inds).append(rec)

    # 模块分（水平）与月变化（ΔMoM）两种口径
    for name, series in module_scores.items():
        for k in (1, 3, 6):
            evaluate(series, name, True)
        delta = {m: series[m] - series[pm]
                 for pm, m in zip(sorted(series), sorted(series)[1:])}
        for k in (1, 3, 6):
            evaluate(delta, f"Δ{name}", True)

    # 单指标分位
    for key, series in ind_pct.items():
        for k in (1, 3, 6):
            evaluate(series, key, False)


def dump(rows: list[dict], path: str):
    if not rows:
        return
    fields = list(rows[0])
    rows = sorted(rows, key=lambda r: -abs(r["ic"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"  写出 {path}（{len(rows)} 行）")


dump(results_modules, os.path.join(OUT, "ic_modules.csv"))
dump(results_inds, os.path.join(OUT, "ic_indicators.csv"))

# ---------------------------------------------------------------------------
# 3. 汇总：全A 3月窗口为主口径
# ---------------------------------------------------------------------------
print("\n== 模块 IC 汇总（中证全A，按 horizon 分列） ==")
def summary(rows: list[dict], bench: str) -> list[dict]:
    by_label: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in rows:
        if r["bench"] == bench:
            by_label[r["label"]][r["horizon"]] = r
    out = []
    for label, hz in by_label.items():
        out.append({
            "label": label,
            "ic1": hz.get(1, {}).get("ic"), "n1": hz.get(1, {}).get("n"),
            "ic3": hz.get(3, {}).get("ic"), "t3": hz.get(3, {}).get("t"),
            "spread3": hz.get(3, {}).get("spread"), "hit3": hz.get(3, {}).get("hit"),
            "ic6": hz.get(6, {}).get("ic"), "n6": hz.get(6, {}).get("n"),
            "n3": hz.get(3, {}).get("n"),
        })
    return sorted(out, key=lambda r: -(abs(r["ic3"]) if r["ic3"] is not None else 0))

for bench in ("wind_all", "hs300"):
    print(f"\n--- {bench} 模块 ---")
    for r in summary(results_modules, bench):
        print(f"  {r['label']:12s} IC1m={r['ic1']:+.2f} IC3m={r['ic3']:+.2f}(t={r['t3']:+.1f},spread={r['spread3']:+.1f}pp,win={r['hit3']:.2f}) IC6m={r['ic6']:+.2f} n={r['n3']}")
    print(f"--- {bench} 指标 TOP15（IC3m） ---")
    for r in summary(results_inds, bench)[:15]:
        print(f"  {r['label']:28s} IC3m={r['ic3']:+.2f}(t={r['t3']:+.1f},spread={r['spread3']:+.1f}pp,win={r['hit3']:.2f}) IC1m={r['ic1']:+.2f} IC6m={r['ic6']:+.2f} n={r['n3']}")

print("\n完成。")
