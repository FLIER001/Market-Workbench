import json
from datetime import datetime
from pathlib import Path
import numpy as np
import bond_priors

ROOT=Path(__file__).resolve().parents[2]


def frozen_inputs():
    config,maps=bond_priors.artifacts()
    curve={'yields':{k:[{'date':d,'v':v} for d,v in m.items()] for k,m in maps.items() if k in ('3月','6月','1年','5年','10年','30年')}}
    auxiliary={'series':{k:{'points':[{'date':d,'v':v} for d,v in maps[k].items()],'error':None} for k in bond_priors.INDEX_JOBS}}
    return curve,auxiliary


def test_current_estimates_reproduce_research_models_including_ensemble():
    curve,auxiliary=frozen_inputs();result=bond_priors.forecast(curve,auxiliary,datetime(2026,10,19))
    for row in result['rows']:
        folder='bonds_short_tenor_2026-10-21' if row['family']=='short_curve' else 'bonds_tuning'
        data=np.load(ROOT/'research/output'/folder/f"predictions_{row['id']}.npz")
        expected=np.mean([data[member][-1] for member in row['members']])
        assert row['status']=='ready',row
        assert abs(row['forecast_bp']-expected)<1e-5,(row['id'],row['forecast_bp'],expected)
        assert all(t['last_label_end']<=row['observed_at'] for t in row['training'])


def test_partial_source_failure_only_suspends_dependent_models_and_old_data_expires():
    c,a=frozen_inputs();a['series']['7-10Y']['error']='offline'
    result=bond_priors.forecast(c,a,datetime(2026,10,19))
    assert {r['group'] for r in result['rows'] if r['status']=='ready'}=={'short','medium'}
    assert all(r['forecast_bp'] is None for r in result['rows'] if r['group']=='long')
    stale=bond_priors.forecast(c,a,datetime(2026,10,30))
    assert all(r['forecast_bp'] is None for r in stale['rows'])
    assert any(r['historical_estimate_bp'] is not None for r in stale['rows'])


def test_last_origin_and_training_never_use_future_observations():
    c,a=frozen_inputs();reference=bond_priors.forecast(c,a,datetime(2026,10,19))
    for rows in c['yields'].values():rows.append({'date':'2030-01-01','v':1000})
    for block in a['series'].values():block['points'].append({'date':'2030-01-01','v':1000})
    altered=bond_priors.forecast(c,a,datetime(2026,10,19))
    assert [r['forecast_bp'] for r in reference['rows']]==[r['forecast_bp'] for r in altered['rows']]
    assert reference['input_fingerprint']==altered['input_fingerprint']


def test_partial_fetch_keeps_original_dates_and_only_failed_slot_error(monkeypatch,tmp_path):
    import bonds,cache_runtime,pandas as pd
    cache_runtime.reset_for_tests()
    old={'date':'2026-10-16','points':[{'date':'2026-10-16','v':100.}], 'fetched_at':'original'}
    cache_runtime.seed('bonds:prior_inputs:v1',{'series':{'7-10Y':old}})
    class AK:
        def bond_new_composite_index_cbond(self,indicator,period):
            if period=='7-10年':raise RuntimeError('source unavailable')
            return pd.DataFrame({'date':['2026-10-20'],'value':[101.]})
    monkeypatch.setattr(bonds.astock,'_akshare',lambda:AK())
    result=bonds._prior_inputs_payload()['series']
    assert result['7-10Y']['fetched_at']=='original'
    assert result['7-10Y']['date']=='2026-10-16'
    assert result['7-10Y']['error']=='source unavailable'
    assert result['3-5Y']['date']=='2026-10-20' and result['3-5Y']['error'] is None
    cache_runtime.reset_for_tests()


def test_published_forecast_archive_is_immutable(monkeypatch,tmp_path):
    import bonds,cache_runtime
    cache_runtime.reset_for_tests();curve,aux=frozen_inputs()
    monkeypatch.setattr(bonds,'DATA_DIR',str(tmp_path))
    monkeypatch.setattr(bonds,'get_curve',lambda force=False:curve)
    cache_runtime.seed('bonds:prior_inputs:v1',aux)
    first=bonds.get_priors();files=list((tmp_path/'bonds_priors').glob('*.json'))
    assert len(files)==1;original=files[0].read_bytes()
    second=bonds.get_priors()
    assert first['input_fingerprint']==second['input_fingerprint']
    assert files[0].read_bytes()==original
    aux['series']['3-5Y']['points'][-1]['v']+=.1
    cache_runtime.seed('bonds:prior_inputs:v1',aux)
    third=bonds.get_priors()
    assert third['input_fingerprint']!=first['input_fingerprint']
    assert len(list((tmp_path/'bonds_priors').glob('*.json')))==2
    assert files[0].read_bytes()==original
    cache_runtime.reset_for_tests()


def test_http_route_exposes_candidate_version_and_forwards_refresh(monkeypatch):
    from fastapi.testclient import TestClient
    import app,bonds
    calls=[]
    monkeypatch.setattr(bonds,'get_priors',lambda force=False:(calls.append(force) or {
        'model_version':'test-candidate-v1','rows':[],'status':'experimental'}))
    # No lifespan context: this route test must not start unrelated schedulers.
    response=TestClient(app.app).get('/api/bonds/priors?refresh=true')
    assert response.status_code==200
    assert response.json()['data']['model_version']=='test-candidate-v1'
    assert calls==[True]


def test_relative_score_neutral_symmetry_and_invalid_volatility():
    assert bond_priors.relative_score(10,10,5)==50
    assert bond_priors.relative_score(15,10,5)>55
    assert bond_priors.relative_score(5,10,5)<45
    assert bond_priors.relative_score(15,10,5)+bond_priors.relative_score(5,10,5)==100
    assert bond_priors.relative_score(10,10,0) is None
    assert bond_priors.relative_score(float('nan'),10,5) is None


def test_score_history_is_prefix_invariant_and_matches_current_estimate():
    c,a=frozen_inputs()
    full=bond_priors.forecast(c,a,datetime(2026,10,19))
    target=next(r for r in full['rows'] if r['id']=='short_index_120')
    point=target['score_history'][-21];cutoff=point['date']
    for rows in c['yields'].values():rows[:]=[p for p in rows if p['date']<=cutoff]
    for block in a['series'].values():block['points']=[p for p in block['points'] if p['date']<=cutoff]
    earlier=bond_priors.forecast(c,a,datetime.fromisoformat(cutoff))
    row=next(r for r in earlier['rows'] if r['id']==target['id'])
    assert row['score_history'][-1]==point
    assert abs(row['forecast_bp']-point['forecast_bp'])<1e-5
    assert row['score']==point['score']
    assert len(target['score_history'])==252
    for row in full['rows']:
        if row['target']=='gov3M':continue
        assert row['score']==row['score_history'][-1]['score']
        assert abs(row['forecast_bp']-row['score_history'][-1]['forecast_bp'])<1e-5
        assert all(p['score'] is None or 0<=p['score']<=100 for p in row['score_history'])
    stale=bond_priors.forecast(c,a,datetime(2026,10,30))
    assert all(r['score'] is None for r in stale['rows'])
    assert any(r['score_history'] for r in stale['rows'])
