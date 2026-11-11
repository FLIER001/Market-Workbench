"""Regression cases from the factor-research review; synthetic inputs only."""
import numpy as np
import pandas as pd
import pytest

import factor_backtest
import factor_pit
import factors


def report(code='600001', rd='2025-03-31', nd='2025-04-01', eps=1.):
    return dict(code=code, report_date=rd, notice_date=nd, eps=eps,
                ocf_ps=.2, roe=3., net_profit=eps, revenue=10.)


def derive(records, dates):
    codes = sorted({r['code'] for r in records})
    return factors._quarter_series_factors(pd.DataFrame(records), pd.DataFrame(10., index=dates, columns=codes))


def test_staggered_announcements_preserve_each_stock_and_missing_resets():
    a = report()
    b = report('600002', nd='2025-04-02', eps=2.)
    missing = report(rd='2025-06-30', nd='2025-08-01', eps=np.nan)
    accr = derive([a, b, missing], ['2025-03-31', '2025-04-01', '2025-04-02', '2025-08-01'])[1]
    assert pd.isna(accr.loc['2025-03-31', '600001'])
    assert accr.loc['2025-04-02', '600001'] == pytest.approx(.8)
    assert accr.loc['2025-04-02', '600002'] == pytest.approx(1.8)
    assert pd.isna(accr.loc['2025-08-01', '600001'])


def test_quarter_dependencies_only_become_visible_on_their_own_announcement():
    dates = ['2025-08-04', '2025-09-01']
    for eps in (.3, .7):
        q1 = report(nd='2025-09-01', eps=eps)
        q2 = report(rd='2025-06-30', nd='2025-08-01', eps=.8)
        accr = derive([q1, q2], dates)[1]
        assert pd.isna(accr.loc[dates[0], '600001'])
        assert accr.loc[dates[1], '600001'] == pytest.approx(.8 - eps)


def test_latest_report_survives_older_correction_and_same_day_ties(monkeypatch):
    records = [report(rd='2024-12-31', nd='2025-06-01'), report(nd='2025-04-25', eps=.3)]
    monkeypatch.setattr(factor_pit, 'pit_table', lambda: pd.DataFrame(records))
    raw = factor_pit.as_of_panel(['2025-06-02'])
    assert raw.loc['2025-06-02', ('600001', 'report_date')] == '2025-03-31'
    assert raw.loc['2025-06-02', ('600001', 'eps')] == .3
    records[0]['notice_date'] = '2025-04-25'
    assert factor_pit.as_of_panel(['2025-04-25']).loc['2025-04-25', ('600001', 'eps')] == .3


def test_rolling_financial_metrics_ignore_not_yet_visible_revisions():
    periods = pd.period_range('2022Q1', periods=12, freq='Q')
    records = [report(rd=str(p.end_time.date()), nd=str((p.end_time + pd.Timedelta(days=30)).date()), eps=1.+i*.2)
               for i, p in enumerate(periods)]
    records[5]['notice_date'] = '2025-01-01'
    before = derive(records, ['2024-02-01'])
    records[5].update(eps=1000., revenue=10000., roe=500., ocf_ps=100.)
    after = derive(records, ['2024-02-01'])
    for left, right in zip(before, after):
        pd.testing.assert_frame_equal(left, right)


class Response:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def api_row(code='600001'):
    return dict(SECURITY_CODE=code, REPORTDATE='2025-03-31', NOTICE_DATE='2025-04-25', BASIC_EPS=.3)


@pytest.mark.parametrize('failure', [None, {'pages': 2, 'count': 2, 'data': []},
                                      {'pages': 2, 'count': 2, 'data': [api_row()]}])
def test_failed_or_duplicate_pages_cannot_return_partial_period(monkeypatch, failure):
    calls = []
    def fetch(*args, **kwargs):
        calls.append(kwargs['params']['pageNumber'])
        result = {'pages': 2, 'count': 2, 'data': [api_row()]} if len(calls) == 1 else failure
        return Response({'result': result})
    monkeypatch.setattr(factor_pit.astock, 'em_get', fetch)
    monkeypatch.setattr(factor_pit.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError):
        factor_pit._fetch_period('2025-03-31')


def test_complete_pages_and_empty_unpublished_period(monkeypatch):
    monkeypatch.setattr(factor_pit.astock, 'em_get', lambda *a, **kw: Response({'result': {
        'pages': 2, 'count': 2, 'data': [api_row('600001' if kw['params']['pageNumber'] == '1' else '600002')]}}))
    assert len(factor_pit._fetch_period('2025-03-31')) == 2
    monkeypatch.setattr(factor_pit.astock, 'em_get', lambda *a, **kw: Response({'result': {'pages': 0, 'count': 0, 'data': []}}))
    assert factor_pit._fetch_period('2025-03-31') == []


def test_legacy_partial_period_is_refetched_and_failure_preserves_files(monkeypatch, tmp_path):
    path = tmp_path / 'fund.csv.gz'
    meta = tmp_path / 'meta.json'
    monkeypatch.setattr(factor_pit, '_FUND_FILE', str(path))
    monkeypatch.setattr(factor_pit, '_FUND_META_FILE', str(meta))
    monkeypatch.setattr(factor_pit.factor_data, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(factor_pit, '_all_periods', lambda: ['2025-03-31'])
    pd.DataFrame([report()]).to_csv(path, compression='gzip', index=False)
    meta.write_text('{"periods":["2025-03-31"]}')
    original = path.read_bytes(), meta.read_bytes()
    def fail(_):
        raise RuntimeError('分页失败')
    monkeypatch.setattr(factor_pit, '_fetch_period', fail)
    with pytest.raises(RuntimeError):
        factor_pit.build_fundamentals()
    assert (path.read_bytes(), meta.read_bytes()) == original
    monkeypatch.setattr(factor_pit, '_fetch_period', lambda _: [api_row(), api_row('600002')])
    assert factor_pit.build_fundamentals()['rows'] == 2
    assert factor_pit._load_meta()['fetch_version'] == 2


def test_intraday_ranges_cannot_change_open_execution_including_sales():
    dates = ['2025-04-01', '2025-04-02', '2025-04-03']
    close = pd.DataFrame({'A': [10., 11., 12.], 'B': [10., 10., 10.]}, index=dates)
    opens = pd.DataFrame(10., index=dates, columns=['A', 'B'])
    candidates = {dates[0]: (1, ['A', 'B']), dates[1]: (1, ['B', 'A'])}
    board = factor_backtest._run_once(candidates, close, opens, opens, opens, dates, set(), 0.)
    moving = factor_backtest._run_once(candidates, close, opens, opens + 1, opens - 1, dates, set(), 0.)
    assert board == moving


def test_known_quarterly_values_match_hand_calculated_metrics():
    periods = pd.period_range('2022Q1', periods=12, freq='Q')
    cumulative = [1, 3, 6, 10, 5, 11, 18, 26, 9, 19, 30, 42]
    records = []
    for i, period in enumerate(periods):
        item = report(rd=str(period.end_time.date()),
                      nd=str((period.end_time + pd.Timedelta(days=30)).date()), eps=cumulative[i])
        item.update(ocf_ps=float(period.quarter), revenue=2. * cumulative[i], roe=float(i+1))
        records.append(item)
    values = [frame.iloc[0, 0] for frame in derive(records, ['2025-02-01'])]
    # Last eight quarterly EPS are 5..12: mean 8.5, sample std sqrt(6).
    assert values == pytest.approx([4 / np.sqrt(6), 11., 8.5 / np.sqrt(6), 8.5 / np.sqrt(6), 1.])


def test_complete_source_pages_can_contain_records_without_pit_dates(monkeypatch, tmp_path):
    missing = api_row('600002')
    missing['NOTICE_DATE'] = None
    monkeypatch.setattr(factor_pit.astock, 'em_get', lambda *a, **kw: Response({'result': {
        'pages': 1, 'count': 2, 'data': [api_row(), missing]}}))
    assert len(factor_pit._fetch_period('2025-03-31')) == 2
    monkeypatch.setattr(factor_pit, '_FUND_FILE', str(tmp_path / 'fund.csv.gz'))
    monkeypatch.setattr(factor_pit, '_FUND_META_FILE', str(tmp_path / 'meta.json'))
    monkeypatch.setattr(factor_pit.factor_data, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(factor_pit, '_all_periods', lambda: ['2025-03-31'])
    assert factor_pit.build_fundamentals()['rows'] == 1
    assert factor_pit._load_meta()['excluded_records'] == {'2025-03-31': 1}
