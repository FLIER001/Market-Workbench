"""Direction API preserves first-seen records without substituting old forecasts."""
from datetime import date, datetime, timedelta
import json
import gold_direction
import gold_score as gold


def setup(monkeypatch,tmp_path):
    rows=[]; day=date(2026,1,1)
    while day<=date(2026,10,20):
        if day.weekday()<5: rows.append((day.isoformat(),100+len(rows)*.2))
        day+=timedelta(days=1)
    monkeypatch.setattr(gold,'_cached_reference_prices',lambda:(rows,'2026-10-21T09:00:00+08:00'))
    monkeypatch.setattr(gold,'_SNAPSHOT',str(tmp_path/'gold.json'))
    original=gold_direction.forecast
    clock=[datetime(2026,10,21,tzinfo=gold.BEIJING)]
    monkeypatch.setattr(gold_direction,'forecast',lambda prices:original(prices,clock[0]))
    return clock


def test_direction_route_and_immutable_first_seen(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    import app
    clock=setup(monkeypatch,tmp_path)
    with TestClient(app.app) as client:
        response=client.get('/api/gold/direction')
    assert response.status_code==200
    first=response.json()['data']
    assert first['status']=='ready' and first['score_is_probability'] is False
    assert first['validation']['model_version']==gold_direction.MODEL_VERSION
    records=list(tmp_path.rglob('*.json')); assert len(records)==1
    content=records[0].read_bytes()
    clock[0]+=timedelta(days=1)
    second=gold.get_direction_forecast()
    assert second['created_at']!=first['created_at']
    assert second['recorded_at']==first['recorded_at']
    assert records[0].read_bytes()==content and len(list(tmp_path.rglob('*.json')))==1
    assert json.loads(content)['prices'][-1][0]==first['observed_at']


def test_missing_input_and_mismatched_summary_are_not_frozen_forecast(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    monkeypatch.setattr(gold,'_cached_reference_prices',lambda:([],None))
    monkeypatch.setattr(gold.market,'_load_json',lambda path:{'model_version':'other'})
    result=gold.get_direction_forecast()
    assert result['status']=='insufficient' and result['score'] is None
    assert result['recorded_at'] is None and result['validation'] is None
    assert not list(tmp_path.rglob('*.json'))


def test_broken_record_is_visible_and_not_overwritten(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path)
    gold.get_direction_forecast()
    record=next(tmp_path.rglob('*.json')); record.write_text('broken')
    result=gold.get_direction_forecast()
    assert result['status']=='ready' and result['record_error']
    assert result['recorded_at'] is None and record.read_text()=='broken'
