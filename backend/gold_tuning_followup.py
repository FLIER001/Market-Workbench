"""Adaptive risk-prior neighborhood analysis after the recorded broad search."""
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
import gold_score as gold
from gold_tuning import dump, summarize

ROOT = Path(__file__).parents[1]
OUT = ROOT/'research/output/gold_tuning_2026-10-20'
DECAYS = (.90,.94,.96,.97,.98,.985,.99,.995,.997)


def ewma_grid(inputs):
    prices = sorted(inputs['reference']['lbma_pm_usd'])
    returns = np.diff(np.log([p for _,p in prices]))
    series = []
    for i in range(120,len(prices)):
        v = {}
        for decay in DECAYS:
            w = decay**np.arange(i-1,-1,-1)
            v[f'ewma/{decay}'] = float(np.sqrt(w@returns[:i]**2/w.sum()*252))
        v['ensemble'] = float(np.sqrt(np.mean([v[f'ewma/{d}']**2 for d in (.94,.97,.99)])))
        series.append((gold._proxy_available_day('g08_momentum',prices[i][0]),v))
    return series


def main():
    out = OUT/'round3';out.mkdir(parents=True,exist_ok=True)
    inputs = json.loads((OUT/'inputs.json').read_text())
    manifest = {'round':3,'status':'adaptive_exploratory','decays':DECAYS,'ensemble':[.94,.97,.99],
                'created_at':datetime.now(timezone.utc).isoformat(),'source_hash':gold._hash_gold(inputs),
                'code_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'protocol_hash':hashlib.sha256((ROOT/'research/gold_tuning_round3_2026-10-20.md').read_bytes()).hexdigest()}
    dump(out/'manifest.json',manifest)
    (out/'research_code.py').write_bytes(Path(__file__).read_bytes())
    rows = json.loads((OUT/'round2/forecasts_macro_vol20.json').read_text())
    series = ewma_grid(inputs)
    days = [d for d,_ in series]
    for r in rows:
        pos = bisect_right(days,r['as_of'])-1
        r['predictions'] = {k:v for k,v in r['predictions'].items() if k in ('persistence20','persistence60','mean')}
        r['predictions'].update(series[pos][1])
    full = summarize(rows,'persistence20')
    yearly = {year:summarize([r for r in rows if r['as_of'].startswith(year)],'persistence20',False)
              for year in sorted({r['as_of'][:4] for r in rows})}
    no2020 = summarize([r for r in rows if not r['as_of'].startswith('2020')],'persistence20',False)
    losses = np.asarray([(r['actual']-r['predictions']['persistence20'])**2 for r in rows])
    trimmed = summarize([r for r,v in zip(rows,losses) if v<=np.quantile(losses,.95)],'persistence20',False)
    qlike={}
    for name in full:
        ratio = np.asarray([r['actual']**2/r['predictions'][name]**2 for r in rows])
        qlike[name] = float(np.mean(ratio-np.log(ratio)-1))
    dump(out/'results.json',{'manifest':manifest,'full':full,'tail26':summarize(rows[-26:],'persistence20',False),
                             'yearly':yearly,'excluding2020':no2020,'trim_top5pct_baseline_errors':trimmed,'qlike':qlike,
                             'interval_scope':'within round3 only; not adjusted for all prior adaptive searches'})
    dump(out/'forecasts.json',rows)
    latest = series[-1][1]
    dump(OUT/'shadow_candidate.json',{
        'created_at':datetime.now(timezone.utc).isoformat(),'status':'research_only',
        'model':'gold-risk-prior-shadow-0.1','target':'next 20 trading-day realized annualized volatility',
        'last_price_observation':inputs['reference']['lbma_pm_usd'][-1][0],
        'historical_results_are_independent':False,'source_hash':gold._hash_gold(inputs),
        'frozen_candidates':{'ewma_097':{'decay':.97,'forecast':latest['ewma/0.97']},
                             'variance_ensemble':{'decays':[.94,.97,.99],'weights':[1/3]*3,'forecast':latest['ensemble']}},
        'postselected_comparator':{'decay':.99,'forecast':latest['ewma/0.99']},
        'live_product_weights_changed':False,
        'tracking_rule':'Append actual first-seen predictions; score only matured future 20-day labels; do not backdate this record.'})
    for name,m in full.items():
        print(name,round(m['r2_vs_primary'],4),'QLIKE',round(qlike[name],4),
              'no2020',round(no2020[name]['r2_vs_primary'],4),'trim',round(trimmed[name]['r2_vs_primary'],4),
              'vs60',round(1-m['mse']/full['persistence60']['mse'],4),flush=True)


if __name__=='__main__':
    main()
