"""Local neighborhood check and concrete frozen direction-prior deliverable."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import numpy as np
import gold_score as gold
from gold_direction import forecast, PARAMETERS
from gold_direction_research import feature_series, walk, directional_metrics, balanced, auc
from gold_tuning import raw_samples,dump,nonoverlap_indices
from gold_research import SEED

ROOT=Path(__file__).parents[1]
OUT=ROOT/'research/output/gold_direction_2026-10-20'
PRIMARY='trend60/weight0.9/shrink0.25'


def bands(rows,name,margin=.02):
    output={}
    for label in ('偏多','中性','偏空'):
        selected=[r for r in rows if ('偏多' if r['probabilities'][name]>=.5+margin else
                  '偏空' if r['probabilities'][name]<=.5-margin else '中性')==label]
        y=np.asarray([r['actual']>0 for r in selected]);actual=[r['actual'] for r in selected]
        output[label]={'n':len(selected),'positive_share':float(y.mean()) if len(y) else None,
                       'mean_log_return':float(np.mean(actual)) if actual else None,
                       'accuracy':float(y.mean() if label=='偏多' else (1-y).mean()) if len(y) and label!='中性' else None}
    selected=[r for r in rows if abs(r['probabilities'][name]-.5)>=margin]
    output['combined']={'n':len(selected),'coverage':len(selected)/len(rows),
        'accuracy':float(np.mean([(r['probabilities'][name]>=.5)==(r['actual']>0) for r in selected])),
        'always_up_same_dates':float(np.mean([r['actual']>0 for r in selected]))}
    return output


def interval(rows,name):
    y=np.asarray([r['actual']>0 for r in rows],dtype=float)
    p=np.asarray([r['probabilities'][name] for r in rows])
    base=np.asarray([r['probabilities']['base'] for r in rows])
    rng=np.random.default_rng(SEED); n=len(rows); block=math.ceil(rows[0]['horizon']/5)+1
    values=[]
    for _ in range(800):
        idx=np.concatenate([(s+np.arange(block))%n for s in rng.integers(0,n,math.ceil(n/block))])[:n]
        ba=balanced(y[idx],p[idx])
        values.append([1-np.mean((p[idx]-y[idx])**2)/np.mean((base[idx]-y[idx])**2),ba])
    return {'brier_skill_ci95':np.quantile(np.asarray(values)[:,0],[.025,.975]).tolist(),
            'balanced_accuracy_ci95':np.quantile(np.asarray(values)[:,1],[.025,.975]).tolist(),
            'block_weeks':block,'scope':'single postselected candidate only; not search-adjusted'}


def main():
    out=OUT/'confirmation';out.mkdir(parents=True,exist_ok=True)
    inputs=json.loads((OUT/'inputs.json').read_text())
    candidates=[{'id':f'trend{n}/weight{w}/shrink{s}','method':'ensemble','group':f'trend{n}/weight{w}',
        'features':[f'mom_risk_{n}','mom_risk_5'],'terms':[(n,1,w),(5,-1,1-w)],
        'window':0,'anchor':'half','shrink':s} for n in (50,60,70) for w in (.85,.9,.95) for s in (.15,.25,.35)]
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'primary':PRIMARY,'parameters':PARAMETERS,
        'candidate_configs':candidates,'source_hash':gold._hash_gold(inputs),'independent_oos':False,
        'code_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'candidate_code_hash':hashlib.sha256((ROOT/'backend/gold_direction.py').read_bytes()).hexdigest(),
        'protocol_hash':hashlib.sha256((ROOT/'research/gold_direction_confirmation_2026-10-20.md').read_bytes()).hexdigest()}
    dump(out/'manifest.json',manifest);(out/'research_code.py').write_bytes(Path(__file__).read_bytes())
    (out/'candidate_code.py').write_bytes((ROOT/'backend/gold_direction.py').read_bytes())
    raw=raw_samples(inputs,feature_series(inputs)); results={}
    for h in (5,20,60):
        for c in candidates:c['horizon']=h
        rows=walk([r for r in raw if r['target']==f'return{h}'],candidates)
        metrics={k:directional_metrics(rows,k) for k in rows[0]['probabilities']}
        primary=metrics[PRIMARY]
        phases=[]
        for idx in nonoverlap_indices(rows):
            selected=[rows[i] for i in idx]
            m=directional_metrics(selected,PRIMARY)
            phases.append({k:m[k] for k in ('n','brier_skill','balanced_accuracy','auc','accuracy','always_up_accuracy')})
        results[str(h)]={'start':rows[0]['as_of'],'end':rows[-1]['as_of'],'metrics':metrics,
            'primary_bands':bands(rows,PRIMARY),'primary_tail52_bands':bands(rows[-52:],PRIMARY),
            'primary_phases':phases,'primary_interval':interval(rows,PRIMARY),
            'brier_skill_vs_half':1-primary['brier']/metrics['half']['brier']}
        dump(out/f'forecasts_{h}.json',rows)
        print(h,{k:primary[k] for k in ('n','balanced_accuracy','auc','brier_skill')},flush=True)
    dump(out/'results.json',{'manifest':manifest,'horizons':results})
    current=forecast(inputs['reference']['lbma_pm_usd'])
    dump(OUT/'frozen_candidate.json',{'candidate':current,'selection':'postselected exploratory weak prior',
        'history_is_independent_oos':False,'primary_key':PRIMARY,
        'do_not_interpret_score_as_probability':True,'live_ui_or_trading_modified':False})


if __name__=='__main__':main()
