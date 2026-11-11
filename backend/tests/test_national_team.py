"""Synthetic checks only: dates, units, missingness and authenticated API."""
from copy import deepcopy
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from national_team import NationalTeamRequest, evaluate, regime, template
import factor_lab_routes


def bundle(inflow=True):
    days = pd.bdate_range(end="2026-10-23", periods=818).date
    market = [{"day": str(d), "available": str(d), "holding_value": float(100 + np.sin(i/20) if i < 755 else 100 + (i-755)*(.01 if inflow else -.01)), "csi800_float_value": 1000} for i, d in enumerate(days)]
    stocks = []
    for i in range(3):
        snapshots = [{"period": "2026-03-31", "disclosed": "2026-04-28", "source": "SYNTHETIC", "float_shares": 1000,
                      "complete": True, "holdings": [{"holder": "中央汇金投资有限责任公司", "platform": "汇金", "shares": 100}]},
                     {"period": "2026-06-30", "disclosed": "2026-08-29", "source": "SYNTHETIC", "float_shares": 1000,
                      "complete": True, "holdings": [{"holder": "中央汇金投资有限责任公司", "platform": "汇金", "shares": 110+i*10}]}]
        prices = [{"day": str(d), "close": float(20 if j < 251 else 10+i), "adjusted_close": float(20 if j < 251 else 10+i)} for j,d in enumerate(days[-252:])]
        stocks.append({"code": f"60000{i}", "name": "SYNTHETIC", "industry": "测试", "eligible": True, "snapshots": snapshots, "prices": prices})
    return {**template(), "previous_signal": "2026-08-28", "top_n": 2, "stocks": stocks, "market": market}


def test_formula_and_audit():
    b = bundle()
    r = evaluate(NationalTeamRequest.model_validate(b))
    assert r["regime"]["state"] == "inflow"
    a = next(x for x in r["rows"] if x["code"] == "600000")
    assert a["change"] == pytest.approx(.01)
    assert a["gap"] == 1
    assert len(r["portfolio"]) == 2
    assert sum(x["weight"] for x in r["portfolio"]) == 1
    assert len(r["input_sha256"]) == 64
    b["mode"] = "paper"
    paper = evaluate(NationalTeamRequest.model_validate(b))
    a = next(x for x in paper["rows"] if x["code"] == "600000")
    assert a["change"] == 110*10-100*20  # 市值变动并非实际买入金额
    assert paper["input_sha256"] != r["input_sha256"]


def test_regime_matches_independent_rolling_computation():
    req = NationalTeamRequest.model_validate(bundle())
    ratios = np.array([p.holding_value/p.csi800_float_value for p in req.market])
    z = [(w[-1]-np.median(w))/np.std(w) for w in [ratios[i:i+756] for i in range(63)]]
    expected = np.cov(np.arange(63), z, ddof=0)[0,1]/np.var(np.arange(63))
    assert regime(req)["slope"] == pytest.approx(expected)


def test_future_disclosures_and_prices_do_not_leak():
    b = bundle()
    before = evaluate(NationalTeamRequest.model_validate(b))
    for s in b["stocks"]:
        future = deepcopy(s["snapshots"][-1])
        future.update(period="2026-09-30", disclosed="2026-10-23")
        future["holdings"][0]["shares"] = 999
        s["snapshots"].append(future)
        s["prices"].append({"day": "2026-10-26", "close": 10000, "adjusted_close": 10000})
    after = evaluate(NationalTeamRequest.model_validate(b))
    assert before["rows"] == after["rows"]


def test_unknown_not_zero_or_other():
    b = bundle()
    b["market"] = []
    r = evaluate(NationalTeamRequest.model_validate(b))
    assert r["regime"]["state"] == "unknown" and not r["portfolio"]
    assert all(x["score"] is None for x in r["rows"])
    b = bundle()
    b["stocks"][0]["snapshots"][0]["holdings"] = []
    r = evaluate(NationalTeamRequest.model_validate(b))
    a = next(x for x in r["rows"] if x["code"] == "600000")
    assert a["change"] is None and a["score"] is None
    b["market"][-1]["available"] = "2026-10-26"
    assert evaluate(NationalTeamRequest.model_validate(b))["regime"]["state"] == "unknown"


def test_other_prefers_light_holdings_and_adjusted_gap():
    b = bundle(False)
    for s in b["stocks"]:
        for p in s["prices"]:
            p["adjusted_close"] = 10  # 模拟原始价格除权下跌，复权路径不变
    r = evaluate(NationalTeamRequest.model_validate(b))
    assert r["regime"]["state"] == "other"
    assert all(x["gap"] == 0 for x in r["rows"])
    assert r["rows"][0]["code"] == "600000"


@pytest.mark.parametrize("mutation", [
    lambda b: b["stocks"].append(deepcopy(b["stocks"][0])),
    lambda b: b["stocks"][0]["prices"].append(b["stocks"][0]["prices"][0]),
    lambda b: b["stocks"][0]["snapshots"][0]["holdings"].append(b["stocks"][0]["snapshots"][0]["holdings"][0]),
    lambda b: b["stocks"][0]["prices"][0].update(close=float("nan")),
    lambda b: b.update(previous_signal=b["as_of"]),
])
def test_invalid_input(mutation):
    b = bundle(); mutation(b)
    with pytest.raises(ValidationError):
        NationalTeamRequest.model_validate(b)


def test_stale_and_partial_and_insufficient_selection():
    b = bundle()
    b["stocks"][0]["snapshots"][-1]["complete"] = False
    b["stocks"][1]["snapshots"] = []
    r = evaluate(NationalTeamRequest.model_validate(b))
    assert r["counts"]["excluded"] == 2
    assert r["status"] == "insufficient_data" and not r["portfolio"]


def test_api_auth_and_real_calculation():
    def auth(request):
        if request.headers.get("x-test-auth") != "yes":
            raise HTTPException(401, "未登录")
        return {"id": 1}
    app = FastAPI(); app.include_router(factor_lab_routes.create_router(auth))
    with TestClient(app) as client:
        path = "/api/factor/research/national-team"
        assert client.get(path+"/template").status_code == 401
        headers = {"x-test-auth": "yes"}
        assert client.get(path+"/template", headers=headers).status_code == 200
        response = client.post(path+"/evaluate", headers=headers, json=bundle())
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]["portfolio"]) == 2
        assert client.post(path+"/evaluate", headers=headers, json={}).status_code == 400
