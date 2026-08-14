"""总分权重设计：候选方案 → 复合得分 → 未来收益检验（含块自助法显著性）。

在 backtest.py 的逐月模块得分基础上，比较三种权重方案：
  A 等权
  B IC 加权（|IC3m| 归一，带符号方向）
  C 手工圆整权重（回测 + 先验混合）
输出复合总分的 IC / 分组价差 / 胜率 / 块自助 p 值，择优写入生产。
"""
from __future__ import annotations

import csv
import math
import os
import random
import sys
from collections import defaultdict

BACKEND = "/Users/k/Vibe-Research/backend"
OUT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND)
os.environ["VR_DATA_DIR"] = os.path.join(OUT, "datadir")
os.makedirs(os.environ["VR_DATA_DIR"], exist_ok=True)

import market  # noqa: E402

# ---- 复用 backtest 的基础件（rank/spearman 已在 backtest.py 验证） ----
sys.path.insert(0, OUT)
from backtest import (load_monthly_close, fwd_returns, spearman,  # noqa: E402
                      IND, rank)

# ---------------------------------------------------------------------------
# 1. 逐月模块得分（同 backtest.py，独立重算避免 import 副作用）
# ---------------------------------------------------------------------------
months = sorted({str(p.get("date", ""))[:7]
                 for card in IND.values() if isinstance(card, dict)
                 for p in card.get("hist") or []
                 if len(str(p.get("date", ""))) >= 7})
months = [m for m in months if m >= "2021-12"]

module_scores: dict[str, dict[str, float]] = defaultdict(dict)
for m in months:
    for mod in market._module_scores(market._indicators_as_of(IND, m), as_of=m):
        if mod.get("score") is not None:
            module_scores[mod["name"]][m] = mod["score"]

# 回测 IC（wind_all, 3m）来自 ic_modules.csv / ic_indicators.csv 的结论：
IC3 = {"财政地产": 0.52, "国内增长与景气": -0.47, "全球外部": 0.30, "价格与工业利润": 0.03}

# ---------------------------------------------------------------------------
# 2. 权重方案。方向 dir=-1 表示该模块对收益为反向（贡献 = 100 - 模块分）。
# ---------------------------------------------------------------------------
SCHEMES: dict[str, dict[str, tuple[float, int]]] = {
    "A_等权": {
        "财政地产": (1, 1), "国内增长与景气": (1, -1), "全球外部": (1, 1), "价格与工业利润": (1, 1),
    },
    "B_IC加权": {
        "财政地产": (0.52, 1), "国内增长与景气": (0.47, -1), "全球外部": (0.30, 1),
        "价格与工业利润": (0.03 * 3, 1),  # 低 IC 项放大 3 倍仍只有 ~0.09 相对权重
    },
    "C_手工": {
        # 回测 |IC| 排序 + 显著性圆整；信用/货币两项无回测历史，给小先验权重
        "财政地产": (30, 1), "国内增长与景气": (25, -1), "全球外部": (20, 1),
        "价格与工业利润": (10, 1), "信用周期": (10, 1), "货币与金融条件": (5, 1),
    },
    "C2_手工_剔弱项": {  # 剔除无预测力的价格模块，只留显著项 + 先验项
        "财政地产": (32, 1), "国内增长与景气": (28, -1), "全球外部": (22, 1),
        "信用周期": (12, 1), "货币与金融条件": (6, 1),
    },
}


def composite(scores_by_module: dict[str, dict[str, float]],
              weights: dict[str, tuple[float, int]]) -> dict[str, float]:
    total_w = sum(w for w, _ in weights.values())
    out: dict[str, float] = {}
    for m in months:
        num, cov = 0.0, 0.0
        for name, (w, sign) in weights.items():
            s = scores_by_module.get(name, {}).get(m)
            if s is None:
                continue
            num += w * ((100.0 - s) if sign < 0 else s)
            cov += w
        if cov >= 0.5 * total_w:
            out[m] = num / cov  # 缺失模块按权重归一（与生产模块引擎同语义）
    return out


def block_bootstrap_p(scores: dict[str, float], rets: dict[str, float],
                      k: int, n_boot: int = 2000, block: int = 3) -> float:
    """对重叠采样做移动块自助，返回双边 p 值（H0: IC=0）。"""
    ms = sorted(m for m in scores if m in rets and k in rets[m])
    x = [scores[m] for m in ms]
    y = [rets[m][k] for m in ms]
    ic0 = spearman(x, y)
    rng = random.Random(42)
    n = len(ms)
    extreme = 0
    for _ in range(n_boot):
        # 对 (score, ret) 成对序列做移动块重排
        sx, sy = [], []
        i = 0
        while i < n:
            start = rng.randrange(n - block + 1)
            sx.extend(x[start:start + block])
            sy.extend(y[start:start + block])
            i += block
        sx, sy = sx[:n], sy[:n]
        ic = spearman(sx, sy)
        if (ic0 >= 0 and ic >= ic0) or (ic0 < 0 and ic <= ic0):
            extreme += 1
    return extreme / n_boot


# ---------------------------------------------------------------------------
# 3. 检验
# ---------------------------------------------------------------------------
BENCH = {"wind_all": "sh000985", "hs300": "sh000300"}
print("== 总分方案对比（复合分 vs 未来收益） ==")
rows = []
for bench, sym in BENCH.items():
    monthly = {m: v for m, v in load_monthly_close(sym).items() if m >= "2021-12"}
    fwd = fwd_returns(monthly)
    ret_months = [m for m in monthly if m < "2026-08"]
    for scheme, weights in SCHEMES.items():
        comp = composite(module_scores, weights)
        for k in (1, 3, 6):
            pairs = [(comp[m], fwd[m][k]) for m in ret_months if m in comp and k in fwd.get(m, {})]
            if len(pairs) < 12:
                continue
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            ic = spearman(xs, ys)
            p = block_bootstrap_p(comp, fwd, k)
            n = len(pairs)
            order = sorted(range(n), key=lambda i: xs[i])
            third = max(n // 3, 1)
            top = [ys[i] for i in order[-third:]]
            bot = [ys[i] for i in order[:third]]
            spread = sum(top) / len(top) - sum(bot) / len(bot)
            rows.append({"bench": bench, "scheme": scheme, "k": k, "n": n,
                         "ic": round(ic, 3), "p_boot": round(p, 3),
                         "spread": round(spread, 2),
                         "win_top": round(sum(1 for r in top if r > 0) / len(top), 2),
                         "win_bot": round(sum(1 for r in bot if r > 0) / len(bot), 2)})

with open(os.path.join(OUT, "composite_schemes.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)

for bench in BENCH:
    print(f"\n--- {bench} ---")
    print(f"  {'scheme':16s} {'k':>2s} {'n':>3s} {'IC':>6s} {'p_boot':>7s} {'spread':>7s} {'win_top':>7s} {'win_bot':>7s}")
    for r in rows:
        if r["bench"] == bench:
            print(f"  {r['scheme']:16s} {r['k']:>2d} {r['n']:>3d} {r['ic']:+6.2f} {r['p_boot']:7.3f} "
                  f"{r['spread']:+7.1f} {r['win_top']:7.2f} {r['win_bot']:7.2f}")

# 各模块 3m 的块自助 p（用于文档披露）
print("\n== 模块 IC3m 块自助显著性（wind_all） ==")
monthly = {m: v for m, v in load_monthly_close("sh000985").items() if m >= "2021-12"}
fwd = fwd_returns(monthly)
for name in ("财政地产", "国内增长与景气", "全球外部", "价格与工业利润"):
    s = module_scores.get(name, {})
    pairs = [m for m in fwd if m in s and m < "2026-08" and 3 in fwd[m]]
    if len(pairs) >= 12:
        p = block_bootstrap_p(s, fwd, 3)
        print(f"  {name:12s} n={len(pairs)} p_boot={p:.3f}")
print("\n完成。")
