"""Research safeguards: future observations/labels cannot alter past forecasts."""
from datetime import date, timedelta
import math
import numpy as np
from gold_tuning import available_series, raw_samples, walk, summarize, nonoverlap_indices
from gold_tuning_followup import ewma_grid


def test_feature_and_label_prefix_is_invariant():
    days = [(date(2018,1,1)+timedelta(days=i)).isoformat() for i in range(650)
            if (date(2018,1,1)+timedelta(days=i)).weekday()<5]
    prices = [(d,1200*math.exp(.0002*i+.01*math.sin(i/8))) for i,d in enumerate(days)]
    inputs = {k:[(d,100+i*.01+math.sin(i/9)) for i,d in enumerate(days)]
              for k in ('real','dollar','stress','etf','cot')}
    inputs['reference']={'lbma_pm_usd':prices}
    cutoff = days[300]
    truncated = {k:[r for r in v if r[0]<=cutoff] for k,v in inputs.items() if k!='reference'}
    truncated['reference']={'lbma_pm_usd':[r for r in prices if r[0]<=cutoff]}
    full = raw_samples(inputs,available_series(inputs))
    short = raw_samples(truncated,available_series(truncated))
    assert short == [r for r in full if r['end_date']<=cutoff]
    for row in full:
        assert max(row['feature_available_at'].values())<=row['as_of']<row['entry_date']<row['end_date']


def test_nested_selection_only_uses_mature_labels():
    rng = np.random.default_rng(7)
    rows=[]
    for i in range(130):
        day = date(2020,1,1)+timedelta(weeks=i)
        rows.append({'as_of':day.isoformat(),'entry_date':(day+timedelta(days=1)).isoformat(),
                     'end_date':(day+timedelta(days=29)).isoformat(),'horizon':20,'target':'return20',
                     'actual':float(rng.normal(.01,.03)), 'features':{'x':float(rng.normal())}})
    configs=[{'id':'ridge','group':'x','features':['x'],'window':52,'alpha':1,'shrink':1}]
    result=walk(rows,configs)
    assert all(r['train_label_end']<r['as_of'] for r in result)
    assert result[0]['selected']=='zero' and result[0]['selection_n']==0
    cutoff=rows[100]['as_of']
    altered=[{**r,'actual':r['actual']+100 if r['end_date']>=cutoff else r['actual']} for r in rows]
    future=walk(altered,configs)
    assert [(r['predictions'],r['selected']) for r in result if r['as_of']<=cutoff] == [
            (r['predictions'],r['selected']) for r in future if r['as_of']<=cutoff]
    summary=summarize(result,'zero')
    assert summary['zero']['r2_vs_primary']==0
    assert summary['nested']['n']==len(result)
    for phase in nonoverlap_indices(result):
        assert all(result[a]['end_date']<=result[b]['entry_date'] for a,b in zip(phase,phase[1:]))


def test_risk_prior_ignores_future_prices():
    prices=[((date(2020,1,1)+timedelta(days=i)).isoformat(),100*math.exp(.0002*i+.02*math.sin(i/8))) for i in range(200)]
    short=ewma_grid({'reference':{'lbma_pm_usd':prices[:160]}})
    full=ewma_grid({'reference':{'lbma_pm_usd':prices}})
    assert short==full[:len(short)]
    assert all(v>0 for _,values in full for v in values.values())
