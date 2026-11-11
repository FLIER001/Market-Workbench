"""Frozen experimental direction prior; no trading or allocation instructions."""
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
import math
from statistics import stdev

MODEL_VERSION='gold-direction-prior-0.1'
PARAMETERS={'horizon':20,'trend_lookback':60,'reversal_lookback':5,
            'volatility_lookback':20,'trend_weight':.9,'reversal_weight':.1,
            'shrink_to_neutral':.25,'bullish_score':52,'bearish_score':48}


def forecast(prices, now=None):
    now=now or datetime.now(timezone(timedelta(hours=8)))
    clean={}
    for day,value in prices:
        try:
            day=date.fromisoformat(str(day)); value=float(value)
        except (TypeError,ValueError): continue
        # Same one-business-day price availability proxy as the research.
        available=day+timedelta(days=1)
        while available.weekday()>=5: available+=timedelta(days=1)
        if available<=now.date() and value>0 and math.isfinite(value): clean[day.isoformat()]=value
    rows=sorted(clean.items())
    result={'model_version':MODEL_VERSION,'status':'insufficient','evidence':'exploratory_weak_prior',
            'independent_oos':False,'created_at':now.isoformat(),'parameters':PARAMETERS.copy(),
            'observed_at':rows[-1][0] if rows else None,'score':None,'direction':'中性',
            'score_is_probability':False,'reason':'至少需要121个可得LBMA PM价格。'}
    if len(rows)<121: return result
    if (now.date()-date.fromisoformat(rows[-1][0])).days>7:
        return {**result,'status':'stale','reason':'价格观测超过7天，暂停新方向倾向。'}
    logs=[math.log(v) for _,v in rows]
    daily=[b-a for a,b in zip(logs,logs[1:])]
    sigma=max(stdev(daily[-20:]),1e-8)
    z60=(logs[-1]-logs[-61])/(sigma*60)*math.sqrt(20)
    z5=-(logs[-1]-logs[-6])/(sigma*5)*math.sqrt(20)
    cdf=lambda z:.5*(1+math.erf(z/math.sqrt(2)))
    raw=.9*cdf(z60)+.1*cdf(z5)
    score=100*(.5+.25*(raw-.5))
    return {**result,'status':'ready','reason':None,'score':score,
            'direction':'偏多' if score>=52 else '偏空' if score<=48 else '中性',
            'trend_return_60':math.expm1(logs[-1]-logs[-61]),
            'short_return_5':math.expm1(logs[-1]-logs[-6]),'daily_volatility_20':sigma,
            'source_hash':hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()}
