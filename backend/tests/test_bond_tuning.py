import numpy as np
from bond_tuning import rolling_mean,ridge_predictions,mask_partition,labels_for,trend_prior


def test_reversal_shrinkage_has_correct_endpoints():
    assert trend_prior(3.,5.,0.,-1)==3.
    assert trend_prior(3.,5.,1.,-1)==-5.
    assert trend_prior(3.,5.,.25,-1)==1.


def test_future_labels_do_not_change_past_predictions():
    rng=np.random.default_rng(7)
    x=rng.normal(size=(650,4));y=rng.normal(size=650);h=20;t=400
    altered=y.copy();altered[t-h:]=100000
    for fn in (lambda v:rolling_mean(v,h,252),lambda v:ridge_predictions(x,v,h,252,10)):
        assert np.allclose(fn(y)[300:t+1],fn(altered)[300:t+1])
        assert not np.isclose(fn(y)[-1],fn(altered)[-1])


def test_batched_ridge_matches_independent_fit():
    rng=np.random.default_rng(42);x=rng.normal(size=(650,4));y=rng.normal(size=650)
    h=20;t=500;window=252;alpha=10
    train=x[t-h-window:t-h];target=y[t-h-window:t-h]
    z=(train-train.mean(axis=0))/train.std(axis=0)
    beta=np.linalg.solve(z.T@z+np.eye(4)*alpha,z.T@(target-target.mean()))
    expected=target.mean()+((x[t]-train.mean(axis=0))/train.std(axis=0))@beta
    assert np.isclose(ridge_predictions(x,y,h,window,alpha)[t],expected)


def test_labels_execution_delay_and_partition_exit():
    values=np.arange(100.,110.)
    assert labels_for(values,2,True)[0]==200
    assert labels_for(values,2,False)[0]==(103/101-1)*10000
    dates=np.array(['2019-12-27','2019-12-30','2019-12-31','2020-01-02','2020-01-03'])
    assert mask_partition(dates,1,'validation2').tolist()==[True,False,False,False,False]


def test_adaptive_weights_only_see_matured_prediction_errors():
    from bond_tuning_adaptive import adaptive_prediction
    rng=np.random.default_rng(4);n=600;h=20
    y=rng.normal(size=n);ps={'mean_756':np.zeros(n),'other':rng.normal(size=n)}
    altered=y.copy();altered[380:]=1000
    a=adaptive_prediction(y,ps,h,252,'top1')
    b=adaptive_prediction(altered,ps,h,252,'top1')
    assert np.allclose(a[200:401],b[200:401])
    assert not np.allclose(a[420:],b[420:])
