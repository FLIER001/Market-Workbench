import asyncio
import json
import time
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import bond_math
import bond_research
import bonds
import cache_runtime


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch, tmp_path):
    cache_runtime.reset_for_tests()
    monkeypatch.setattr(bonds, 'DATA_DIR', str(tmp_path))
    yield
    cache_runtime.reset_for_tests()


def curve(date='2026-10-19', value=2.):
    return {'date': date, 'curve': [{'tenor': k, 'value': value} for k in bond_math.TENOR_YEARS],
            'source': 'test curve', 'generated_at': '2026-10-19T18:00:00+08:00'}


def test_flat_curve_cashflows_and_funding_horizon():
    row = next(r for r in bond_math.scenario_rows(curve()['curve'], 1.) if r['tenor'] == '10年')
    # Independent closed form: a par bond before its first coupon accrues by
    # sqrt(1+y/2), not a full annual yield spread.
    assert row['gross_carry_bp_3m'] == pytest.approx(((1.01)**.5-1)*10000, abs=.001)
    assert row['carry_bp_3m'] == pytest.approx(24.876, abs=.001)
    assert row['roll_bp_3m'] == 0
    assert row['modified_duration'] == pytest.approx((1-1.01**-20)/.02, abs=.001)
    assert row['breakeven_bp_3m'] == pytest.approx(2.825, abs=.002)
    assert row['scenarios'][0]['gross_bp'] > row['gross_static_bp_3m'] > row['scenarios'][-1]['gross_bp']


def test_principal_matures_within_horizon_and_zero_yields():
    for y in (0., -0.5, 2.):
        flows = bond_math.par_flows(.25, y/100)
        assert bond_math.price(flows, y/100) == pytest.approx(100)
        row = next(r for r in bond_math.scenario_rows(curve(value=y)['curve'], None) if r['tenor']=='3月')
        assert row['total_static_bp_3m'] is None
        assert row['breakeven_bp_3m'] is None
        assert row['scenarios'][0]['gross_bp'] == row['scenarios'][-1]['gross_bp']


def test_missing_or_different_date_funding_is_not_zero_or_proxy_alignment():
    for funding in ({}, {'date': '2026-10-16', 'series': {'O/N': [{'v': 1.}]}}):
        result = bonds._calc_payload(curve(), funding)
        assert result['funding_cost'] is None
        assert all(r['total_static_bp_3m'] is None for r in result['rows'])
    result = bonds._calc_payload(curve(), {'date': '2026-10-19', 'series': {'O/N': [{'v': 1.}]}})
    assert result['rows'][-1]['total_static_bp_3m'] is not None


def test_midrank_constants_ties_and_nonfinite():
    assert bonds._pct([{'v': 1.4}]*8) == .5
    assert bonds._score_from_pct(bonds._pct([{'v': 1.4}]*8), 'down') == 0
    assert bonds._midrank([1,2,2,3],2) == .5
    assert bonds._pct([{'v': float('nan')}]*20) is None
    assert bonds._pct([{'v': float('inf')}]*20) is None


def test_asof_hist_no_month_daily_composition_flip_or_last_point_override():
    parts = [{'hist': [{'date': '2026-09', 'v': 2}], 'weight': .4, 'max_age_days': 150},
             {'hist': [{'date': '2026-09-30', 'v': -2}, {'date': '2026-10-01', 'v': -2}], 'weight':1.6, 'max_age_days':10}]
    result = bonds._segment_hist(parts, ['2026-09-29','2026-09-30','2026-10-01'])
    assert result == [{'date':'2026-09-30','v':-1.2},{'date':'2026-10-01','v':-1.2}]
    # A supplied publication timestamp is honored over the observation period.
    assert bonds._available_date({'date':'2026-09','release_at':'2026-10-15T09:30:00+08:00'}) == '2026-10-15'
    assert bonds._segment_hist(parts, ['2027-01-01']) == []


def test_credit_is_deduplicated_and_financing_not_directionally_scored():
    hist = [{'date':f'2026-10-{i:02d}','v':i} for i in range(1,10)]
    state = bonds._credit_state({'credit': {'AAA-3年':hist}}, {'credit_spread_aaa':{'hist':hist}})
    assert [p['key'] for p in state['parts']] == ['AAA-3年']
    policy = bonds._policy_state({'social_financing_stock':{'hist':hist},'special_bond_issuance':{'hist':hist}})
    assert policy == {}


def test_futures_filter_keeps_dates_and_values_aligned(monkeypatch):
    dates = pd.bdate_range('2026-01-01', periods=65)
    df = pd.DataFrame({'日期': dates, '持仓量':range(100,165), '成交量':range(200,265), '收盘价':[102.]*65})
    df.loc[30,'持仓量'] = float('nan')
    class AK:
        def futures_main_sina(self, **kwargs): return df
    monkeypatch.setattr(bonds.astock, '_akshare', lambda:AK())
    result = bonds._positioning_payload()
    h = result['contracts'][0]['oi_hist']
    assert len(h) == 64
    assert h[30] == {'date':str(dates[31])[:10],'v':131.}


def test_legacy_snapshot_retains_unknown_generation_and_expires(tmp_path):
    path = tmp_path/'bonds_curve.json'
    path.write_text(json.dumps({'date':'2026-10-16','curve':[{'tenor':'10年','value':2.}]}))
    assert bonds._snapshot_time('curve') == 0
    assert bonds._load_snapshot('curve')['legacy_snapshot'] is True
    assert bonds._load_snapshot('curve')['date'] == '2026-10-16'


def test_dashboard_same_input_fingerprint_for_all_blocks(monkeypatch):
    c = curve()
    base = {'curve':c,'funding':{'date':c['date'],'series':{'O/N':[{'v':1.}]}},'policy':{},'index':{},'global':{},'positioning':{}}
    monkeypatch.setattr(bonds,'_overview_payload',lambda force=False:{'_inputs':base,**base})
    monkeypatch.setattr(bonds.market,'get_macro',lambda:{'cn':{}})
    monkeypatch.setattr(bonds,'get_ib_overnight',lambda:{})
    result = bonds._dashboard_payload()
    assert {result[k]['input_fingerprint'] for k in ('framework','calc','segments')} == {result['input_fingerprint']}
    assert len(result['segments']['rows']) == 6
    assert all(r['score'] is None for r in result['segments']['rows'])


def test_ai_never_reuses_another_snapshot_and_does_not_persist_raced_result(monkeypatch):
    payload={'input_fingerprint':'new','segments':{'rows':[{'segment':'短债'}]},'ai_insight':{'ranking':'old','segments':{'短债':'old'},'input_fingerprint':'old'}}
    monkeypatch.setattr(bonds,'get_dashboard',lambda:payload)
    async def fail(_payload): return ''
    monkeypatch.setattr(bonds,'_build_insight',fail)
    assert asyncio.run(bonds.get_ai_insight()) == ''
    cache_runtime.seed('bonds:dashboard:v2',{'input_fingerprint':'newer'})
    async def build(_payload): return {'ranking':'new','segments':{'短债':'new'},'input_fingerprint':'new'}
    monkeypatch.setattr(bonds,'_build_insight',build)
    asyncio.run(bonds.get_ai_insight())
    assert 'ai_insight' not in cache_runtime.peek('bonds:dashboard:v2')


def test_dependency_revision_rebuilds_without_forcing_source_refetch(monkeypatch):
    previous={'curve':curve(),'dependency_versions':{'bonds:curve':'old'},'dependency_health':{'bonds:curve':['fresh',None]}}
    cache_runtime.seed('bonds:dashboard:v2',previous)
    monkeypatch.setattr(bonds,'get_curve',lambda:curve())
    calls=[]
    monkeypatch.setattr(bonds,'_dashboard_payload',lambda force=False:(calls.append(force) or {'curve':curve(),'input_fingerprint':'revised'}))
    bonds.get_dashboard()
    for _ in range(100):
        if calls: break
        time.sleep(.001)
    assert calls == [False]


def test_research_training_purge_and_future_values_cannot_change_past_predictions():
    rng = np.random.default_rng(5)
    x = rng.normal(size=(180,6)); y=rng.normal(size=180)
    p = bond_research.walk_forward(x,y,20,min_train=30)
    assert all(r['train_last_label_end'] <= r['origin'] for r in p)
    x2=x.copy(); y2=y.copy(); x2[120:]=1e8; y2[120:]=1e8
    q = bond_research.walk_forward(x2,y2,20,min_train=30)
    a={r['origin']:r['ridge'] for r in p if r['origin']<120}
    b={r['origin']:r['ridge'] for r in q if r['origin']<120}
    assert a == b


def test_transaction_cost_sensitivity_and_liquidation():
    pred=[{'origin':t,'ridge':1.,'mean':1.,'actual':1.} for t in range(50)]
    gross=np.full(80,.01); finance=np.zeros(80)
    result=bond_research.proxy_portfolio(pred,[],gross,finance,5)
    values=[r['cumulative_excess_pct'] for r in result if r['model']=='ridge']
    assert values == sorted(values,reverse=True)
    assert all(r['turnover']==2 for r in result)


def test_vintage_archive_preserves_original_input_and_old_fetch_time(tmp_path):
    value={'schema_version':2,'generated_at':'2026-10-20T10:00:00+08:00','input_fingerprint':'abc',
           'input_vintage':{'date':'2026-10-16','release_at':None,'fetched_at':None}}
    bonds._save_snapshot('dashboard',value)
    value['input_vintage']['date']='2026-10-19'
    bonds._save_snapshot('dashboard',value)
    vintage=json.loads((tmp_path/'bonds_vintages/2026-10-20-abc.json').read_text())
    assert vintage['inputs']['date']=='2026-10-16'
    assert vintage['inputs']['release_at'] is None
    assert vintage['inputs']['fetched_at'] is None


def test_stale_daily_observations_use_calendar_or_explicit_weekday_fallback():
    now=datetime(2026,10,20,10,tzinfo=bonds.BEIJING)
    assert bonds._status('2026-10-16','daily',now)[0]=='stale'
    assert bonds._status('2026-10-19','daily',now)[0]=='fresh'
    assert '近似' in bonds._status('2026-10-19','daily',now)[1]


def test_source_failure_does_not_block_other_sources(monkeypatch):
    monkeypatch.setattr(bonds,'get_curve',lambda force=False:(_ for _ in ()).throw(RuntimeError('curve down')))
    monkeypatch.setattr(bonds,'get_shibor',lambda force=False:{'date':'2026-10-19','series':{'O/N':[{'v':1.}]}})
    for name in ('policy','index','global','positioning'):
        monkeypatch.setattr(bonds,f'get_{name}',lambda force=False:{})
    result=bonds._overview_payload()
    assert result['funding']['series']['O/N'][0]['v']==1.
    assert 'curve down' in result['refresh_error']
    assert result['calc']=={}
