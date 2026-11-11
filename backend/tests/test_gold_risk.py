"""Risk UI uses the frozen research formula, honest missingness and no future rows."""
from datetime import datetime, date, timedelta
import json
import math
from pathlib import Path
import pytest
import gold_score as gold


def prices():
    return [((date(2025,1,1)+timedelta(days=i)).isoformat(),100*math.exp(.0003*i+.02*math.sin(i/7))) for i in range(190)]


def test_risk_ensemble_matches_frozen_formula_and_future_is_ignored():
    rows=prices(); now=datetime.fromisoformat(rows[159][0]).replace(tzinfo=gold.BEIJING)
    result=gold.calculate_gold_risk(rows,now)
    assert result==gold.calculate_gold_risk(rows[:160],now)
    returns=[math.log(b[1]/a[1]) for a,b in zip(rows[:159],rows[1:160])]
    variances=[]
    for decay in (.94,.97,.99):
        weights=[decay**j for j in range(len(returns)-1,-1,-1)]
        variances.append(252*sum(w*r*r for w,r in zip(weights,returns))/sum(weights))
    assert result['annualized_volatility']==pytest.approx(math.sqrt(sum(variances)/3))
    assert result['horizon_volatility']==pytest.approx(result['annualized_volatility']*math.sqrt(20/252))
    assert not result['independent_oos']


def test_missing_or_stale_prices_never_reuse_frozen_research_forecast():
    assert gold.calculate_gold_risk([])['annualized_volatility'] is None
    assert gold.calculate_gold_risk(prices()[:100])['status']=='insufficient'
    p=gold.calculate_gold_risk(prices(),datetime(2026,10,20,tzinfo=gold.BEIJING))
    assert p['status']=='stale' and p['annualized_volatility'] is None


def test_risk_endpoint_only_reads_cached_reference(monkeypatch):
    from fastapi.testclient import TestClient
    import app
    rows=prices(); today=date.today().isoformat()
    rows.append((today,rows[-1][1]*1.01))
    monkeypatch.setitem(gold.market._SUB,'gold:wgc_reference_v3',(0,{'lbma_pm_usd':rows}))
    monkeypatch.setattr(gold.market,'_load_json',lambda path: None)
    with TestClient(app.app) as client:
        response=client.get('/api/gold/risk')
    assert response.status_code==200
    assert response.json()['data']['status']=='ready'
    assert response.json()['data']['validation'] is None
    packaged=json.loads((Path(gold.__file__).parent/'data/gold_risk_validation.json').read_text())
    assert packaged['model_version']==gold.RISK_MODEL_VERSION
    assert packaged['mse_improvement_vs20']==pytest.approx(.2857251479257596)
