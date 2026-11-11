"""Gold model invariants: no missing-data uplift, future data, or history rewrite."""
from copy import deepcopy
from datetime import datetime, date, timedelta
import json
import math
import pytest
import gold_score as g


@pytest.fixture(scope='module')
def inputs():
    days=[]; d=date(2018,1,1)
    while d<=date(2026,10,19):
        if d.weekday()<5: days.append(d.isoformat())
        d+=timedelta(days=1)
    price=[(d,1200*math.exp(.0003*i+.03*math.sin(i/35)+.005*math.sin(i/2))) for i,d in enumerate(days)]
    weekly=[(d,2500+2*i+80*math.sin(i/10)) for i,d in enumerate(days[4::5])]
    return {'real':[(d,1+.3*math.sin(i/80)) for i,d in enumerate(days)],
            'dollar':[(d,100+3*math.sin(i/70)) for i,d in enumerate(days)],
            'etf':weekly, 'cot':[(d,15+8*math.sin(i/12)) for i,(d,_) in enumerate(weekly)],
            'stress':[(d,math.sin(i/30)) for i,d in enumerate(days)],
            'cb':[(f'{y}-{m:02}',30000+(y-2009)*120+m*10) for y in range(2009,2027) for m in range(1,13) if f'{y}-{m:02}'<='2026-08'],
            'fx':[(d,7.0) for d in days],
            'real_sources':['gold:fred:DFII10'],
            'reference':{'lbma_pm_usd':price,'lbma_am_usd':price,
                         'sge_pm_cny':[(d,(p+10+8*math.sin(i/40))*7/31.1034768) for i,(d,p) in enumerate(price)]}}


def statuses():
    return [{'key':k,'status':'fresh','usable':True,'label':k} for k in g._SOURCE_LABELS]


def calculate(inputs):
    return g.calculate_gold(inputs, statuses(), datetime(2026,10,20,tzinfo=g.BEIJING))


def test_complete_score_contributions_and_missing_gate(inputs):
    p=calculate(inputs)
    assert p['score_status']=='complete'
    assert p['coverage']==100
    assert abs(50+sum(c['points'] for c in p['contributions'])-p['gold_score'])<.01
    for key in ('real','dollar','etf','cot','stress','cb','fx'):
        broken={**inputs,key:[]}
        q=calculate(broken)
        assert q['gold_score'] is None and q['score_status']=='insufficient', key
        assert q['contributions']==[]
    assert calculate({**inputs,'reference':{}})['gold_score'] is None


def test_current_score_is_same_function_as_history_tail(inputs):
    p=calculate(inputs)
    for ind in p['indicators']:
        last=p['_observation_histories'][ind['key']][-1]
        assert last[0]==ind['signal_date']
        assert abs(last[1]-ind['score'])<.001


def test_future_appends_do_not_change_past(inputs):
    p=calculate(inputs)
    extended=deepcopy(inputs)
    extended['real'].append(('2027-01-01',99))
    extended['reference']['lbma_pm_usd'].append(('2027-01-01',99999))
    q=calculate(extended)
    assert p['gold_score']==q['gold_score']
    assert p['_observation_histories']==q['_observation_histories']


def test_today_staleness_does_not_reweight_historical_curve(inputs):
    p=calculate(inputs)
    states=statuses()
    states[0]['status']='stale';states[0]['usable']=False
    states[1]['status']='stale';states[1]['usable']=False
    q=g.calculate_gold(inputs,states,datetime(2026,10,20,tzinfo=g.BEIJING))
    assert p['hist']==q['hist']
    assert q['gold_score'] is None


def test_no_fx_before_history_and_no_imputed_price(inputs):
    shortened={**inputs,'fx':inputs['fx'][1000:]}
    q=calculate(shortened)
    ind=next(i for i in q['indicators'] if i['key']=='g07_sglb')
    assert min(p['date'] for p in ind['signal_hist'])>=shortened['fx'][0][0]
    assert calculate({**inputs,'fx':[]})['gold_score'] is None


def test_midrank_calendar_window_and_warmup():
    assert g._pct_rank([0.0]*30,0.0)==.5
    rows=[((date(2010,1,1)+timedelta(days=7*i)).isoformat(),float(i)) for i in range(700)]
    scores=g._score_history(rows)
    end=rows[-1][0]
    cutoff=date.fromisoformat(end).replace(year=int(end[:4])-5).isoformat()
    expected=g._pct_rank([v for d,v in rows if d>=cutoff],rows[-1][1])*100
    assert abs(scores[-1][1]-expected)<1e-8
    assert g._score_history(rows[:100])==[]


def test_observation_months_never_become_month_start():
    assert g._proxy_available_day('g06_cb','2026-08')>'2026-10-01'
    assert g._proxy_available_day('g04_comex','2026-10-13')>'2026-10-16'
    with pytest.raises(ValueError):
        g._dimension_score_history([('g06_cb',1)],{'g06_cb':[('2026-08',80)]},{'g06_cb':1})


def test_snapshot_immutable_dedupe_and_last_complete(inputs,tmp_path,monkeypatch):
    monkeypatch.setattr(g,'_SNAPSHOT',str(tmp_path/'snapshot.json'))
    monkeypatch.setattr(g,'_RECORD_DIR',str(tmp_path/'records'))
    monkeypatch.setattr(g,'_INPUT_DIR',str(tmp_path/'inputs'))
    p=g._persist_gold(calculate(inputs),inputs)
    record=next((tmp_path/'records').glob('*.json'));before=record.read_bytes()
    g._persist_gold(calculate(inputs),inputs)
    assert record.read_bytes()==before and len(list((tmp_path/'records').glob('*.json')))==1
    q=g._persist_gold(calculate({**inputs,'real':[]}),{**inputs,'real':[]})
    assert q['gold_score'] is None
    assert q['last_complete_score']['gold_score']==p['gold_score']
    assert len(list((tmp_path/'inputs').glob('*.json')))==2


def test_research_training_labels_precede_forecasts():
    import gold_research as r
    rows=[]
    for i in range(110):
        day=date(2020,1,1)+timedelta(days=i*7)
        rows.append({'as_of':day.isoformat(),'end_date':(day+timedelta(days=60)).isoformat(),
                     'actual':math.sin(i)/10,'features':{'expert':40+i/10}})
    forecasts=r.walk_forward(rows)
    assert forecasts
    assert all(x['train_label_end']<x['as_of'] and x['train_n']>=52 for x in forecasts)
    assert r.corr([1]*30,list(range(30))) is None
    assert r.block_interval(list(range(10)),list(range(10)),4) is None


def test_commodity_missing_gold_propagates_to_anchor(monkeypatch):
    import timing_alloc as t
    monkeypatch.setattr(t,'_bond_bond_side',lambda s:(50,'',[]))
    monkeypatch.setattr(t,'_carry_score',lambda s:(50,''))
    scores=t._asset_scores({},'2026-10-20',50,{}, {},{}, {'oil_score':90},
                           {'gold_score':None,'score_status':'insufficient'}, {})
    assert scores['commodity']['score'] is None
    assert scores['commodity']['data_status']=='insufficient'
    anchor={'equity':40,'bond':40,'commodity':10,'cash':10}
    out,resolved,_=t._resolve_weights(anchor,scores,.1)
    assert not resolved and out==anchor
    assert '输入不足' in t._explain_assets(scores,{'cash_floor':.1},{})['commodity']['meaning']


def test_valid_empty_and_legacy_warm_are_not_current(monkeypatch):
    empty={'model_version':g.MODEL_VERSION,'indicators':[],'score_status':'insufficient'}
    monkeypatch.setattr(g.market,'_load_json',lambda p:empty)
    assert g._load_gold_snapshot()==empty
    old={'schema_version':3,'gold_score':90,'indicators':[1]}
    monkeypatch.setattr(g.market,'_layered_get',lambda *a,**kw:old)
    public=g.get_gold_score()
    assert public['gold_score'] is None and public['score_status']=='legacy'
    assert public['last_complete_score']['gold_score']==90


def test_cached_score_expires_without_successful_rebuild(monkeypatch):
    data={'model_version':g.MODEL_VERSION,'score_status':'complete','gold_score':80,
          'source_status':[{'key':'fx','active':True,'latest_period':'2000-01-01','max_age_days':5}],
          'indicators':[{'key':'x','sources':['fx'],'weight':1,'effective_weight':1,'data_status':'fresh'}]}
    monkeypatch.setattr(g.market,'_layered_get',lambda *a,**kw:data)
    out=g.get_gold_score()
    assert out['gold_score'] is None and out['coverage']==0
    assert out['last_complete_score']['gold_score']==80


def test_http_research_and_query_expose_insufficiency(monkeypatch):
    import app, tools
    from fastapi.testclient import TestClient
    client=TestClient(app.app)
    monkeypatch.setattr(g,'get_validation_summary',lambda:{'model_version':g.MODEL_VERSION,'status':'research_only'})
    assert client.get('/api/gold/research').json()['data']['status']=='research_only'
    monkeypatch.setattr(g,'get_gold_score',lambda:{'gold_score':None,'score_status':'insufficient','coverage':60})
    assert tools._gold_score({})['score_status']=='insufficient'


def test_failed_persistence_never_claims_published(inputs,tmp_path,monkeypatch):
    monkeypatch.setattr(g,'_SNAPSHOT',str(tmp_path/'snapshot.json'))
    monkeypatch.setattr(g,'_RECORD_DIR',str(tmp_path/'records'))
    monkeypatch.setattr(g,'_INPUT_DIR',str(tmp_path/'inputs'))
    monkeypatch.setattr(g.market,'_save_json',lambda *a,**kw:None)
    with pytest.raises(OSError,match='not persisted'):
        g._persist_gold(calculate(inputs),inputs)


def test_ai_does_not_return_text_from_previous_input(monkeypatch):
    import asyncio
    import cache_runtime
    snapshot={'score_status':'complete','state_id':'new','indicators':[1],
              'updated':'2026-10-20T18:00:00+08:00','ai_input_id':'old',
              'ai_insight':{'opportunity_cost':'old','flows_positioning':'old','overall':'old'}}
    cache_runtime.seed('gold_score_v3',snapshot)
    monkeypatch.setattr(g,'get_gold_score',lambda *a,**kw:snapshot)
    async def failed(*a,**kw): return ''
    monkeypatch.setattr(g,'_build_insight',failed)
    assert asyncio.run(g.get_ai_insight())==''
    cache_runtime.invalidate('gold_score_v3')
