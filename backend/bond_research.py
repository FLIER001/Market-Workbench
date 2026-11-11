"""Reproducible, retrospective bond walk-forward study; never a live signal.

python backend/bond_research.py --input-dir PATH --output-dir PATH [--fetch-segments]
Specification is written before fitting. Fixed ridge penalty, expanding training
with at most 756 observations, 252 minimum labels, next-observation execution,
purged labels, and a final chronological holdout (no parameter selection).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SPEC = {
    "version": 1, "horizons": [20, 60], "min_train": 252, "max_train": 756,
    "ridge_penalty": 10., "final_holdout_fraction": .3, "execution_lag": 1,
    "features": ["10Y_level", "10Y-1Y", "2*5Y-1Y-10Y", "30Y-10Y", "10Y_change_5d", "10Y_change_20d"],
    "bootstrap_repetitions": 500, "bootstrap_seed": 20261020,
    "cost_bp_per_weight_change": [0, 2, 5, 10],
    "claim_rule": "No production predictive claim from revised prices alone or missing execution/cost/vintage evidence",
}


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def series(points) -> dict[str, float]:
    out = {}
    for p in points or []:
        d, v = str(p.get("date", ""))[:10], p.get("v")
        if len(d) == 10 and isinstance(v, (float, int)) and np.isfinite(v):
            if datetime.fromisoformat(d).weekday() < 5:
                out[d] = float(v)
    return out


def feature_panel(curve: dict):
    histories = curve.get("yields") or {}
    maps = {k: series(histories.get(k)) for k in ("1年", "5年", "10年", "30年")}
    dates = sorted(set.intersection(*(set(v) for v in maps.values())))
    if len(dates) < 21:
        return [], np.empty((0, 6)), np.array([])
    values = np.array([[maps[k][d] for k in maps] for d in dates])
    one, five, ten, thirty = values.T
    features = np.column_stack([ten, ten-one, 2*five-one-ten, thirty-ten,
                                ten - np.roll(ten, 5),
                                ten - np.roll(ten, 20)])
    return dates[20:], features[20:], ten[20:]


def walk_forward(features, labels, horizon, min_train=252, max_train=756):
    """At origin t, only labels ending by t are admitted; t+1 entry."""
    predictions = []
    for t in range(len(features)):
        eligible = np.flatnonzero(np.isfinite(labels[:max(0, t-horizon)]))
        eligible = eligible[-max_train:]
        if len(eligible) < min_train or not np.isfinite(labels[t]):
            continue
        x, y = features[eligible], labels[eligible]
        center, scale = x.mean(axis=0), x.std(axis=0)
        scale[scale < 1e-10] = 1
        train = np.column_stack([np.ones(len(x)), (x-center)/scale])
        penalty = np.eye(train.shape[1]) * SPEC["ridge_penalty"]
        penalty[0, 0] = 0
        beta = np.linalg.solve(train.T @ train + penalty, train.T @ y)
        pred = float(np.r_[1., (features[t]-center)/scale] @ beta)
        predictions.append({"origin": t, "train_last_origin": int(eligible[-1]),
                            "train_last_label_end": int(eligible[-1]+horizon+1),
                            "actual": float(labels[t]), "zero": 0., "mean": float(y.mean()), "ridge": pred})
    return predictions


def bootstrap_mean_ci(values, block, repetitions=500):
    values = np.asarray(values)
    if not len(values):
        return [None, None]
    rng = np.random.default_rng(SPEC["bootstrap_seed"])
    block = min(block, len(values))
    estimates = []
    for _ in range(repetitions):
        starts = rng.integers(0, len(values), size=int(np.ceil(len(values)/block)))
        idx = np.concatenate([(s + np.arange(block)) % len(values) for s in starts])[:len(values)]
        estimates.append(float(values[idx].mean()))
    return [round(float(x), 6) for x in np.quantile(estimates, [.025, .975])]


def evaluate(predictions, horizon, target, dates, unit):
    results = []
    if not predictions:
        return [{"target": target, "horizon": horizon, "status": "insufficient_training_labels", "n": 0}]
    split = int(len(predictions)*(1-SPEC["final_holdout_fraction"]))
    for part, rows in (("development", predictions[:split]), ("final_holdout", predictions[split:])):
        if not rows:
            continue
        y = np.array([r["actual"] for r in rows])
        zero_loss = y*y
        mean_loss = (y-np.array([r["mean"] for r in rows]))**2
        for model in ("zero", "mean", "ridge"):
            p = np.array([r[model] for r in rows])
            loss = (y-p)**2
            denom = zero_loss.sum()
            directional = p != 0
            results.append({"target": target, "horizon": horizon, "partition": part,
                            "model": model, "unit": unit, "n": len(rows),
                            "start": dates[rows[0]["origin"]], "end": dates[rows[-1]["origin"]],
                            "rmse": round(float(np.sqrt(loss.mean())), 4), "mae": round(float(np.abs(y-p).mean()), 4),
                            "oos_r2_vs_zero": round(float(1-loss.sum()/denom), 5) if denom > 0 else None,
                            "oos_r2_vs_mean": round(float(1-loss.sum()/mean_loss.sum()), 5) if mean_loss.sum() > 0 else None,
                            "direction_accuracy": round(float(((p[directional]>0)==(y[directional]>0)).mean()), 4) if directional.any() else None,
                            "mse_improvement_95ci": bootstrap_mean_ci(zero_loss-loss, horizon),
                            "mse_improvement_vs_mean_95ci": bootstrap_mean_ci(mean_loss-loss, horizon),
                            "nonoverlap_n": len(rows[::horizon]),
                            "nonoverlap_rmse": round(float(np.sqrt(loss[::horizon].mean())), 4),
                            "status": "retrospective_only"})
    return results


def proxy_portfolio(predictions, dates, gross, finance, horizon):
    """Nonoverlapping, long-or-cash segment-index proxy. No shorting or leverage."""
    if not predictions:
        return []
    start = int(len(predictions)*(1-SPEC["final_holdout_fraction"]))
    rows, next_entry = [], -1
    for r in predictions[start:]:
        entry = r["origin"]+1
        if entry < next_entry:
            continue
        rows.append(r)
        next_entry = entry+horizon
    out = []
    for model in ("buy_hold", "mean", "ridge"):
        for cost in SPEC["cost_bp_per_weight_change"]:
            equity, peak, drawdown, previous, turnover = 1., 1., 0., 0., 0.
            pnl = []
            for r in rows:
                i = r["origin"]
                weight = 1. if model == "buy_hold" or r[model] > 0 else 0.
                change = abs(weight-previous)
                turnover += change
                # Uninvested capital earns the same funding proxy; report EXCESS
                # return so being in cash has zero excess payoff.
                net = weight*(gross[i]-finance[i])-change*cost/10000
                pnl.append(net)
                equity *= 1+net
                peak = max(peak, equity)
                drawdown = min(drawdown, equity/peak-1)
                previous = weight
            if rows:
                # Liquidation at the final endpoint is included in cost.
                equity *= 1-previous*cost/10000
                drawdown = min(drawdown, equity/peak-1)
                turnover += previous
            out.append({"model": model, "cost_bp": cost, "trades_n": len(rows),
                        "turnover": round(turnover, 2), "cumulative_excess_pct": round((equity-1)*100, 4),
                        "max_drawdown_excess_pct": round(drawdown*100, 4),
                        "caveat": "分段财富指数与Shibor代理，不含指数复制误差、抵押折扣和实际成交冲击；累计为超额收益复利近似"})
    return out


def fetch_segments(directory: Path):
    import bonds
    errors = {}
    data = {}
    for name, period in (("3-5Y", "3-5年"), ("7-10Y", "7-10年")):
        try:
            df = bonds._call_with_timeout(bonds.astock._akshare().bond_new_composite_index_cbond,
                                          indicator="财富", period=period, timeout=35)
            data[name] = [{"date": str(r["date"])[:10], "v": float(r["value"])} for _, r in df.iterrows()]
        except Exception as e:
            errors[name] = str(e)
    directory.mkdir(parents=True, exist_ok=True)
    (directory/"bonds_segment_indices.json").write_text(json.dumps({"series": data, "errors": errors,
        "source": "中债新综合财富指数/期限分段（AKShare）", "fetched_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))


def run(input_dir: Path, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    # Specification is durable before any fitting or performance examination.
    (output_dir/"protocol.json").write_text(json.dumps(SPEC, indent=2))
    curve = load(input_dir/"bonds_curve.json")
    funding = load(input_dir/"bonds_shibor.json")
    segments = load(input_dir/"bonds_segment_indices.json")
    dates, features, ten = feature_panel(curve)
    results, all_predictions, economics = [], {}, []
    for h in SPEC["horizons"]:
        labels = np.full(len(dates), np.nan)
        for t in range(len(dates)-h-1):
            labels[t] = (ten[t+h+1]-ten[t+1])*100
        pred = walk_forward(features, labels, h, SPEC["min_train"], SPEC["max_train"])
        results.extend(evaluate(pred, h, "10Y_yield_change", dates, "bp"))
        all_predictions[f"yield_{h}"] = pred
    funding_map = series((funding.get("series") or {}).get("O/N"))
    for name in ("3-5Y", "7-10Y"):
        index = series((segments.get("series") or {}).get(name))
        for h in SPEC["horizons"]:
            labels = np.full(len(dates), np.nan)
            gross = np.full(len(dates), np.nan)
            finance = np.full(len(dates), np.nan)
            for t in range(len(dates)-h-1):
                entry, exit = t+1, t+1+h
                span = dates[entry:exit+1]
                if not all(d in index and d in funding_map for d in span):
                    continue
                gross[t] = index[dates[exit]]/index[dates[entry]]-1
                finance[t] = sum(funding_map[a]/100*(datetime.fromisoformat(b)-datetime.fromisoformat(a)).days/365 for a, b in zip(span, span[1:]))
                labels[t] = (gross[t]-finance[t])*10000
            pred = walk_forward(features, labels, h, SPEC["min_train"], SPEC["max_train"])
            target = f"{name}_wealth_excess"
            results.extend(evaluate(pred, h, target, dates, "bp"))
            all_predictions[f"{name}_{h}"] = pred
            economics.append({"target": target, "horizon": h, "results": proxy_portfolio(pred, dates, gross, finance, h)})
    fingerprints = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in input_dir.glob("bonds_*.json")}
    report = {"status": "retrospective_price_only", "generated_at": datetime.now(timezone.utc).isoformat(),
              "conclusion": "预测能力未获实盘验证；已完成可得价格数据的回溯滚动检验。宏观初值版本、实际成交/融资与成本仍不足。",
              "data_start": dates[0] if dates else None, "data_end": dates[-1] if dates else None,
              "observations": len(dates), "protocol": SPEC, "input_sha256": fingerprints,
              "source": "中债曲线/分段财富指数；Shibor O/N为融资代理", "results": results,
              "economics": economics, "predictions": all_predictions,
              "limitations": ["历史价格为当前可得版本，非逐日归档初值；不能保证真实发布时间。",
                              "只运行价格信息模型；宏观实时初值版本不足，未参与模型。",
                              "20/60日重叠窗口使用区块bootstrap和非重叠子样本；短样本置信区间不稳定。",
                              "净收益只是指数与融资代理的成本敏感性，不能等同可执行策略收益。",
                              "Carry/Roll是假想债曲线基准，与分段指数成分不等同，未混用为同一可交易基准。"],
              "segment_fetch_errors": segments.get("errors", {})}
    (output_dir/"result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
    lines = ["# 债市预测验证执行结果", "", report["conclusion"], "", f"有效价格样本：{report['data_start']}—{report['data_end']}，{len(dates)}个观测日。", "",
             "|目标|期限|模型|最终测试样本|RMSE bp|OOS R²对零变化/零超额|OOS R²对历史均值|误差改善95%区间（相对均值）|", "|---|---:|---|---:|---:|---:|---:|---|"]
    for r in results:
        if r.get("partition") == "final_holdout":
            lines.append(f"|{r['target']}|{r['horizon']}|{r['model']}|{r['n']}|{r['rmse']}|{r['oos_r2_vs_zero']}|{r['oos_r2_vs_mean']}|{r['mse_improvement_vs_mean_95ci']}|")
        elif r.get("status") == "insufficient_training_labels":
            lines.append(f"|{r['target']}|{r['horizon']}|数据不足|0|—|—|—|")
    lines += ["", "## 证据限制", ""] + [f"- {x}" for x in report["limitations"]]
    lines += ["", "模型未调参，最后30%预测记录为保留测试区；所有预测、输入哈希、训练截止和成本敏感性保存在result.json。正式统计结论仍需更长、可核验的实时版本与真正执行数据。"]
    (output_dir/"报告.md").write_text("\n".join(lines)+"\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1]/"research/output/bonds_validation")
    parser.add_argument("--fetch-segments", action="store_true")
    args = parser.parse_args()
    if args.fetch_segments:
        fetch_segments(args.input_dir)
    report = run(args.input_dir, args.output_dir)
    print(json.dumps({"status": report["status"], "observations": report["observations"], "result_rows": len(report["results"]), "fetch_errors": report["segment_fetch_errors"]}, ensure_ascii=False))
