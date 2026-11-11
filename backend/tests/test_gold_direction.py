"""Direction research cannot learn from labels that have not matured."""
from datetime import date, timedelta
import math
import numpy as np
from gold_direction_research import fit_probability, walk, auc, balanced, feature_series
from gold_direction import forecast
from bisect import bisect_right
import pytest


def test_classifier_and_online_selection_ignore_future_labels():
    rows=[]
    for i in range(120):
        d=date(2020,1,3)+timedelta(weeks=i)
        rows.append({'as_of':d.isoformat(),'entry_date':(d+timedelta(days=3)).isoformat(),
                     'end_date':(d+timedelta(days=31)).isoformat(),'horizon':20,
                     'actual':.01 if math.sin(i/5)>0 else -.01,
                     'features':{'x':math.cos(i/7)}})
    config={'id':'logit','method':'logit','group':'test','features':['x'],'window':52,'alpha':1,'shrink':1}
    bins={'id':'bins','method':'bins','group':'x','features':['x'],'window':104,'strength':30}
    result=walk(rows,[config,bins]); cutoff=rows[100]['as_of']
    altered=[{**r,'actual':-r['actual'] if r['end_date']>=cutoff else r['actual']} for r in rows]
    after=walk(altered,[config,bins])
    assert [(r['probabilities'],r['selected']) for r in result if r['as_of']<=cutoff]==[
           (r['probabilities'],r['selected']) for r in after if r['as_of']<=cutoff]
    assert all(r['train_label_end']<r['as_of'] for r in result)
    assert all(0<p<1 for r in result for p in r['probabilities'].values())
    assert 0<fit_probability(rows,{'x':10000},config)<1


def test_direction_metrics_do_not_reward_always_up_as_balanced_skill():
    y=np.asarray([0,0,1,1,1,1],dtype=float)
    assert balanced(y,np.ones(6)*.7)==.5
    assert auc(y,np.ones(6)*.7)==.5
    assert auc(y,y)==1
    assert auc(y,1-y)==0


def test_new_price_features_have_prefix_invariance():
    days=[(date(2020,1,1)+timedelta(days=i)).isoformat() for i in range(220)]
    prices=[(d,100*math.exp(.0002*i+.02*math.sin(i/7))) for i,d in enumerate(days)]
    def inputs(n):
        return {**{key:[(d,100+i*.01) for i,d in enumerate(days[:n])]
                   for key in ('real','dollar','etf','cot','stress')},
                'reference':{'lbma_pm_usd':prices[:n]}}
    short=feature_series(inputs(170)); full=feature_series(inputs(220))
    for key in ('rsi14','z20','mom_5','ma60_risk','vol_ratio'):
        assert short[key]==full[key][:len(short[key])]


def test_frozen_candidate_matches_research_and_never_uses_future_prices():
    days=[];d=date(2025,1,1)
    while len(days)<230:
        if d.weekday()<5:days.append(d.isoformat())
        d+=timedelta(days=1)
    prices=[(d,100*math.exp(.0003*i+.03*math.sin(i/9))) for i,d in enumerate(days)]
    inputs={**{key:[(d,100+i*.01) for i,d in enumerate(days)] for key in ('real','dollar','etf','cot','stress')},
            'reference':{'lbma_pm_usd':prices}}
    series=feature_series(inputs);as_of=days[180];features={}
    for key in ('mom_risk_60','mom_risk_5'):
        values=series[key];pos=bisect_right([d for d,_ in values],as_of)-1;features[key]=values[pos][1]
    config={'method':'ensemble','window':0,'terms':[(60,1,.9),(5,-1,.1)],'horizon':20,
            'features':['mom_risk_60','mom_risk_5']}
    expected=.5+.25*(fit_probability([],features,config)-.5)
    now=__import__('datetime').datetime.fromisoformat(as_of)
    result=forecast(prices,now)
    assert result['score']==pytest.approx(100*expected)
    assert result==forecast([p for p in prices if p[0]<=as_of],now)
    assert 37.5<=result['score']<=62.5
    assert result['score_is_probability'] is False
    assert forecast([])['score'] is None
    assert forecast(prices,__import__('datetime').datetime(2026,10,20))['status']=='stale'
