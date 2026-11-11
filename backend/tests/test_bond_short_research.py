import numpy as np
import pytest
from bond_short_research import short_labels,partition,feature_groups,economics
from bond_tuning import ridge_predictions


@pytest.mark.parametrize('h',[1,3,5,10])
@pytest.mark.parametrize('lag',[0,1])
def test_short_labels_and_purge_are_aligned(h,lag):
    values=np.arange(100.,800.)
    y=short_labels(values,h,lag,True)
    assert y[30]==pytest.approx((values[30+h+lag]-values[30+lag])*100)
    assert np.isnan(y[-h-lag:]).all()
    rng=np.random.default_rng(4);x=rng.normal(size=(len(y),3));t=500
    altered=y.copy();altered[t-h-lag+1:]=10000
    a=ridge_predictions(x,y,h+lag-1,252,10)
    b=ridge_predictions(x,altered,h+lag-1,252,10)
    assert np.allclose(a[400:t+1],b[400:t+1])


def test_partition_rejects_labels_extending_past_cutoff():
    ds=np.array(['2019-12-27','2019-12-30','2019-12-31','2020-01-02'])
    assert partition(ds,1,0,'validation2').tolist()==[True,True,False,False]
    assert partition(ds,1,1,'validation2').tolist()==[True,False,False,False]


def test_short_features_are_causal():
    n=400;rng=np.random.default_rng(9)
    panel={'dates':np.array([str(x)[:10] for x in np.arange(np.datetime64('2019-01-01'),np.datetime64('2019-01-01')+np.timedelta64(n,'D'))]),
           'yield10Y':3+rng.normal(0,.01,n).cumsum(),'curve':rng.normal(size=(n,4)),
           '3-5Y':100+rng.normal(0,.1,n).cumsum(),'7-10Y':100+rng.normal(0,.1,n).cumsum()}
    altered={k:v.copy() for k,v in panel.items()}
    for k in ('yield10Y','curve','3-5Y','7-10Y'):altered[k][301:]+=100
    a=feature_groups(panel,panel['3-5Y']);b=feature_groups(altered,altered['3-5Y'])
    for k in a:assert np.allclose(a[k][100:301],b[k][100:301])


def test_economic_costs_use_short_bond_returns_and_charge_switches():
    long=np.array([10.,-10.,10.,-10.]);short=np.ones(4)
    rows=economics(long,short,np.array([1.,-1.,1.,-1.]),np.ones(4,dtype=bool),1)
    def row(model,cost):return next(r for r in rows if r['model']==model and r['cost_bp']==cost)
    assert row('selected',0)['return_pct']==pytest.approx(((1.001*1.0001)**2-1)*100)
    assert row('selected',2)['turnover']==4
    assert row('selected',2)['return_pct']<row('selected',0)['return_pct']
    assert row('short_hold',5)['return_pct']==row('short_hold',0)['return_pct']
