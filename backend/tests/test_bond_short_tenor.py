import numpy as np
from bond_short_tenor import asof_quotes,features_for,economics


def test_funding_asof_never_borrows_future_quotes_or_invents_same_day_quote():
    ds=np.array(['2018-12-27','2018-12-28','2018-12-31','2019-01-10'])
    values,ages,observed=asof_quotes(ds,{'2018-12-28':2.,'2019-01-02':3.})
    assert np.isnan(values[0]) and np.isnan(values[-1])
    assert values[2]==2 and ages[2]==3 and observed[2]=='2018-12-28'
    assert observed[0] is None and observed[-1] is None


def test_short_tenor_features_causal_and_not_ten_year_momentum_copy():
    rng=np.random.default_rng(3);n=500
    ys={k:2+rng.normal(0,.01,n).cumsum() for k in ('gov3M','gov6M','gov1Y','gov10Y')}
    fs={k:(2+rng.normal(0,.01,n).cumsum(),np.zeros(n)) for k in ('隔夜','3月')}
    a=features_for(ys,fs,ys['gov3M'],True,None,None)
    changed={k:v.copy() for k,v in ys.items()}
    for v in changed.values():v[301:]+=10
    b=features_for(changed,fs,changed['gov3M'],True,None,None)
    for k in a:assert np.allclose(a[k][100:301],b[k][100:301])
    assert np.allclose(a['momentum'][100:,0],(ys['gov3M'][100:]-ys['gov3M'][95:-5])*100)


def test_constant_positive_short_index_forecast_is_just_hold_not_alpha():
    y=np.full(20,10.);p=np.ones(20);base=np.ones(20);mask=np.ones(20,dtype=bool)
    rows=economics(y,p,base,mask,5)
    for phase in range(5):
        for cost in (0.,.5,1.,2.,5.):
            selected=next(r for r in rows if r['model']=='selected' and r['phase']==phase and r['cost_bp']==cost)
            hold=next(r for r in rows if r['model']=='buy_hold' and r['phase']==phase and r['cost_bp']==cost)
            assert selected['return_pct']==hold['return_pct']
            assert selected['turnover']==2
