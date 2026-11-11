"""Offline, preregistered gold forecast diagnostics. No network or production writes.

python backend/gold_research.py --inputs frozen-inputs.json --output output-dir
An optional --publish-summary path exports the small summary for an isolated or
explicitly selected runtime. It never modifies user runtime by default.
"""
from __future__ import annotations
import argparse
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import numpy as np
import gold_score as gold

SEED = 20261020


def rank(values):
    values = np.asarray(values, dtype=float)
    order = np.argsort(values, kind='stable')
    ranks = np.empty(len(values), dtype=float)
    i = 0
    while i < len(values):
        j = i + 1
        while j < len(values) and values[order[j]] == values[order[i]]:
            j += 1
        ranks[order[i:j]] = (i + j - 1)/2
        i = j
    return ranks


def corr(x, y):
    if len(x) < 10 or np.std(x) < 1e-10 or np.std(y) < 1e-10:
        return None
    return float(np.corrcoef(rank(x), rank(y))[0, 1])


def block_interval(x, y, block, repeats=400):
    if len(x) < max(20, block*3) or corr(x, y) is None:
        return None
    rng = np.random.default_rng(SEED)
    values = []
    for _ in range(repeats):
        idx = np.concatenate([(start+np.arange(block)) % len(x)
                              for start in rng.integers(0, len(x), math.ceil(len(x)/block))])[:len(x)]
        c = corr(np.asarray(x)[idx], np.asarray(y)[idx])
        if c is not None:
            values.append(c)
    return [float(v) for v in np.quantile(values, [.025, .975])] if values else None


def samples(inputs, histories, horizons=(5,20,60)):
    prices = sorted(inputs['reference']['lbma_pm_usd'])
    days = [d for d, _ in prices]
    keys = list(gold._META)
    hdays = {k:[d for d, _ in histories.get(k, [])] for k in keys}
    weeks = {}
    for i, (d, _) in enumerate(prices):
        weeks[datetime.fromisoformat(d).isocalendar()[:2]] = i
    result = []
    for i in sorted(weeks.values()):
        day = days[i]
        values = {}
        for key in keys:
            pos = bisect_right(hdays[key], day)-1
            age = (datetime.fromisoformat(day)-datetime.fromisoformat(hdays[key][pos])).days if pos>=0 else 999
            max_age = 100 if key=='g06_cb' else 14 if key in ('g02_dollar','g03_etf','g04_comex') else 7
            if pos < 0 or age > max_age:
                break
            values[key] = histories[key][pos][1]
        if len(values) != len(keys) or i+1 >= len(prices):
            continue
        entry = i+1
        features = {'momentum':values['g08_momentum'],
                    'rates_fx':(values['g01_real_rate']*.25+values['g02_dollar']*.15)/.4,
                    'equal':sum(values.values())/len(values),
                    'expert':sum(values[k]*gold._META[k][2] for k in keys)}
        features.update({'without_'+k:sum(values[j]*gold._META[j][2] for j in keys if j!=k)/(1-gold._META[k][2]) for k in keys})
        for h in horizons:
            if entry+h >= len(prices):
                continue
            path = np.asarray([p for _,p in prices[entry:entry+21]], dtype=float)
            drawdown = float(np.min(path/np.maximum.accumulate(path)-1)) if len(path)==21 else None
            result.append({'as_of':day, 'entry_date':days[entry], 'end_date':days[entry+h], 'horizon':h,
                           'actual':math.log(prices[entry+h][1]/prices[entry][1]), 'max_drawdown_20':drawdown,
                           'features':features, 'indicator_scores':values})
    return result


def walk_forward(rows):
    output = []
    for i, row in enumerate(rows):
        train = [r for r in rows[:i] if r['end_date'] < row['as_of']]
        if len(train) < 52:
            continue
        y = np.asarray([r['actual'] for r in train])
        predictions = {'zero':0.0, 'mean':float(y.mean())}
        for name, value in row['features'].items():
            x = np.asarray([r['features'][name] for r in train])
            design = np.column_stack([np.ones(len(x)), (x-50)/50])
            beta = np.linalg.lstsq(design, y, rcond=None)[0]
            predictions[name] = float(beta[0]+beta[1]*(value-50)/50)
        output.append({**row,'train_n':len(train),'train_label_end':max(r['end_date'] for r in train),'predictions':predictions})
    return output


def metrics(rows, horizon):
    if not rows:
        return {}
    y = np.asarray([r['actual'] for r in rows])
    base = np.asarray([r['predictions']['mean'] for r in rows])
    base_mse = float(np.mean((y-base)**2))
    output = {}
    for name in rows[0]['predictions']:
        p = np.asarray([r['predictions'][name] for r in rows])
        mse = float(np.mean((y-p)**2))
        output[name] = {'n':len(rows), 'mse':mse, 'r2_vs_mean':1-mse/base_mse if base_mse>0 else None,
                        'direction_accuracy':float(np.mean((p>0)==(y>0))) if name!='zero' else None,
                        'rank_correlation':corr(p,y),
                        'rank_ci95_block':block_interval(p,y,math.ceil(horizon/5))}
    output['_unconditional_positive_share'] = float(np.mean(y>0))
    output['_mean_log_return'] = float(np.mean(y))
    score = np.asarray([r['features']['expert'] for r in rows])
    output['_expert_score_ic'] = corr(score,y)
    output['_expert_score_ic_ci95'] = block_interval(score,y,math.ceil(horizon/5))
    dd_rows = [r for r in rows if r.get('max_drawdown_20') is not None]
    output['_drawdown20_n'] = len(dd_rows)
    output['_expert_score_drawdown20_ic'] = corr([r['features']['expert'] for r in dd_rows], [r['max_drawdown_20'] for r in dd_rows])
    bands = []
    for lo,hi in [(0,20),(20,45),(45,65),(65,80),(80,101)]:
        values = [r['actual'] for r in rows if lo <= r['features']['expert'] < hi]
        bands.append({'band':f'{lo}-{hi}', 'n':len(values), 'mean_log_return':float(np.mean(values)) if values else None})
    output['_bands'] = bands
    return output


def evaluate(inputs):
    last_day = max(d for d,_ in inputs['reference']['lbma_pm_usd'])
    payload = gold.calculate_gold(inputs, [], datetime.fromisoformat(last_day).replace(tzinfo=timezone.utc))
    all_rows = samples(inputs, payload['_research_histories'])
    # Same untouched terminal calendar segment for every horizon, including censored labels.
    holdout_start = (datetime.fromisoformat(last_day)-__import__('datetime').timedelta(weeks=26)).date().isoformat()
    report, forecasts = {}, []
    for h in (5,20,60):
        oos = walk_forward([r for r in all_rows if r['horizon']==h])
        for r in oos:
            r['split'] = 'final_holdout' if r['as_of']>=holdout_start else 'rolling_research'
        forecasts.extend(oos)
        report[str(h)] = {'rolling_research':metrics([r for r in oos if r['split']=='rolling_research'],h),
                          'final_holdout':metrics([r for r in oos if r['split']=='final_holdout'],h),
                          'final_n':sum(r['split']=='final_holdout' for r in oos)}
    return {'model_version':gold.MODEL_VERSION,'status':'research_only','independent_oos':False,
            'evaluated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'sample_count':len({r['as_of'] for r in forecasts}), 'source_as_of':last_day, 'holdout_start':holdout_start,
            'horizons':report, 'candidate_count':14, 'bootstrap_seed':SEED,
            'source_hash':gold._hash_gold(inputs),
            'model_code_hash':hashlib.sha256(Path(gold.__file__).read_bytes()).hexdigest(),
            'research_code_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'protocol_hash':hashlib.sha256((Path(__file__).parents[1]/'research/gold_validation_protocol_v3.md').read_bytes()).hexdigest(),
            'conclusion':'仅使用最新修订数据和保守发布延迟代理；原始发布时间及修订版本不足，不能证实实时预测能力。最终留出每期限样本不足26时，不作稳定性判断。',
            'limits':['COT/ETF/央行等实际发布日、停摆和历史修订未完整恢复',
                      '尾段曾在实现纠错中查看，不能视为独立未触碰的最终检验',
                      '候选区间未作多重比较校正；不按最佳结果选模型',
                      '没有交易仓位与成本，方向命中不等于策略胜率',
                      '零收益与价格随机游走在该目标上等价'],
            'forecasts':forecasts}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--publish-summary')
    args=parser.parse_args()
    inputs=json.loads(Path(args.inputs).read_text())
    report=evaluate(inputs)
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    forecasts=report.pop('forecasts')
    (out/'forecasts.json').write_text(json.dumps(forecasts,ensure_ascii=False,indent=2,allow_nan=False))
    (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    lines=['# 黄金V3重构预测研究', '', report['conclusion'],'',f"实际可评估周时点 {report['sample_count']}；固定候选14个。",'',
           '|期限|分段|n|先验模型R² vs均值|动量R²|先验方向命中|无条件上涨率|','|---|---|---:|---:|---:|---:|---:|']
    for h, groups in report['horizons'].items():
        for split in ('rolling_research','final_holdout'):
            m=groups[split]
            if not m: lines.append(f'|{h}日|{split}|0|—|—|—|—|');continue
            ex=m['expert'];mo=m['momentum']
            lines.append(f"|{h}日|{split}|{ex['n']}|{ex['r2_vs_mean']:.3f}|{mo['r2_vs_mean']:.3f}|{ex['direction_accuracy']:.1%}|{m['_unconditional_positive_share']:.1%}|")
    lines+=['','区块相关区间、全部候选及删项结果见 results.json；逐笔预测和训练标签截止日见 forecasts.json。',
            '该结果不包含严格原始发布版本，未给出可交易收益或策略Sharpe。']
    (out/'研究结果.md').write_text('\n'.join(lines)+'\n')
    summary={k:report[k] for k in ('model_version','status','independent_oos','evaluated_at','sample_count','conclusion','source_as_of','source_hash')}
    summary['horizons'] = {h: {k: m.get('final_holdout',{}).get('expert',{}).get(k) for k in ('n','r2_vs_mean','direction_accuracy')}
                           for h,m in report['horizons'].items()}
    for h,m in report['horizons'].items():
        summary['horizons'][h]['unconditional_positive_share'] = m.get('final_holdout',{}).get('_unconditional_positive_share')
    summary['artifact']=str((out/'results.json').resolve())
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    if args.publish_summary:
        Path(args.publish_summary).parent.mkdir(parents=True,exist_ok=True)
        Path(args.publish_summary).write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print('\n'.join(lines))

if __name__=='__main__':
    main()
