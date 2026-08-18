"""PIT 财务数据层：东财业绩报表（RPT_LICO_FN_CPD）按报告期全量拉取，2006-至今。

Point-in-time 纪律：
- 每条记录有 报告期（REPORTDATE）和 实际公告日（NOTICE_DATE）；
- 因子取数时按「交易日 T 可见的最新一期」对齐：NOTICE_DATE ≤ T 的最大报告期；
- 绝不用报告期直接对齐交易日（那是前视偏差——4 月 30 日的年报属 12-31 报告期）。

数据边界：
- 指标集 = 业绩报表披露口径（营收/净利/EPS/ROE/同比/毛利率/每股现金流/每股净资产），
  非完整三表；资产负债率等字段此接口没有；
- NOTICE_DATE 是东财存的「最新公告日」，历史上更正公告会覆盖该日期——对
  2006-2015 的老数据，少数记录的可见时点可能比真实首次公告晚（修正偏差，
  偏保守方向：不会提前看到未来数据，但个别记录可见得偏晚）。

存储：~/.vibe-research/factor/fundamentals.csv.gz（code,report_date,notice_date,指标列）
+ fundamentals_meta.json（已抓报告期清单，断点续传）。
# ponytail: 整包重建约 82 期 × ~10 页 ≈ 800 请求，10-20 分钟；要增量日更再改追加模式
"""
from __future__ import annotations

import json
import os
import threading
import time

import pandas as pd

import astock
import factor_data

_FUND_FILE = os.path.join(factor_data.DATA_DIR, "fundamentals.csv.gz")
_FUND_META_FILE = os.path.join(factor_data.DATA_DIR, "fundamentals_meta.json")
PIT_START_YEAR = 2006  # 新会计准则口径起点；更早的混口径价值低

# 东财字段 → 本地字段（业绩报表披露口径的全部可用指标）
_COLS = {
    "TOTAL_OPERATE_INCOME": "revenue",       # 营业总收入（元）
    "PARENT_NETPROFIT": "net_profit",        # 归母净利润（元）
    "BASIC_EPS": "eps",                      # 每股收益
    "BPS": "bps",                            # 每股净资产
    "WEIGHTAVG_ROE": "roe",                  # 加权平均 ROE（%）
    "YSTZ": "rev_yoy",                       # 营收同比增长（%）
    "SJLTZ": "profit_yoy",                   # 净利润同比增长（%）
    "XSMLL": "gross_margin",                 # 销售毛利率（%）
    "MGJYXJJE": "ocf_ps",                    # 每股经营现金流
    "DEDUCT_BASIC_EPS": "eps_deducted",      # 扣非每股收益
}

_STATE_LOCK = threading.Lock()
_STATE: dict = {"building": False, "periods_done": 0, "periods_total": 0, "rows": 0,
                "started_at": None, "done_at": None, "error": None}


def pit_state() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


def _all_periods() -> list[str]:
    """2006 年至今全部报告期（3/6/9/12 月末），按时间升序。"""
    periods = []
    now = time.localtime()
    for year in range(PIT_START_YEAR, now.tm_year + 1):
        for md in ("03-31", "06-30", "09-30", "12-31"):
            period = f"{year}-{md}"
            if (year, md) <= (now.tm_year, f"{now.tm_mon:02d}-{now.tm_mday:02d}"):
                periods.append(period)
    return periods


def _fetch_period(report_date: str) -> list[dict]:
    """拉一个报告期全部股票（500 行/页，东财上限）。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    rows: list[dict] = []
    page = 1
    while page <= 60:
        params = {
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            "pageSize": "500", "pageNumber": str(page),
            "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
            "filter": f"(REPORTDATE='{report_date}')",
        }
        d = astock.em_get(url, params=params, timeout=25).json()
        res = d.get("result") or {}
        data = res.get("data") or []
        if not data:
            break
        rows.extend(data)
        if page >= (res.get("pages") or 1):
            break
        page += 1
    return rows


def _row_to_record(row: dict) -> dict | None:
    code = str(row.get("SECURITY_CODE") or "")
    notice = str(row.get("NOTICE_DATE") or "")[:10]
    if len(code) != 6 or not code.isdigit() or not notice:
        return None
    rec = {
        "code": code,
        "report_date": str(row.get("REPORTDATE") or "")[:10],
        "notice_date": notice,
    }
    for em, local in _COLS.items():
        v = row.get(em)
        rec[local] = float(v) if isinstance(v, (int, float)) else None
    return rec


def build_fundamentals() -> dict:
    """全量构建 PIT 财务表（断点续传：已抓报告期跳过）。"""
    with _STATE_LOCK:
        if _STATE["building"]:
            return {"already_building": True}
        _STATE.update(building=True, periods_done=0, rows=0, error=None,
                      started_at=time.strftime("%Y-%m-%d %H:%M:%S"), done_at=None)

    try:
        os.makedirs(factor_data.DATA_DIR, exist_ok=True)
        meta = _load_meta()
        periods = _all_periods()
        with _STATE_LOCK:
            _STATE["periods_total"] = len(periods)

        all_records: list[dict] = list(_load_existing_records()) if meta["periods"] else []
        for period in periods:
            if period in meta["periods"]:
                with _STATE_LOCK:
                    _STATE["periods_done"] += 1
                continue
            records = [r for r in (_row_to_record(x) for x in _fetch_period(period)) if r]
            all_records.extend(records)
            meta["periods"].append(period)
            with _STATE_LOCK:
                _STATE["periods_done"] += 1
                _STATE["rows"] = len(all_records)
            _save_meta(meta)  # 每期落一次，中断可续
        if not all_records:
            raise RuntimeError("财务数据全部报告期拉取为空")

        df = pd.DataFrame(all_records).drop_duplicates(subset=["code", "report_date"], keep="last")
        df.sort_values(["code", "report_date"], inplace=True)
        built_at = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = _FUND_FILE + ".tmp"
        df.to_csv(tmp, index=False, compression="gzip")
        os.replace(tmp, _FUND_FILE)

        with _STATE_LOCK:
            _STATE.update(building=False, done_at=built_at)
        factor_data._fund_cache_clear()
        return {
            "built_at": built_at, "rows": int(len(df)), "stocks": int(df["code"].nunique()),
            "report_periods": len(periods),
            "notice_date_min": str(df["notice_date"].min()), "notice_date_max": str(df["notice_date"].max()),
        }
    except Exception as exc:  # noqa: BLE001 — 状态机必须收敛
        with _STATE_LOCK:
            _STATE.update(building=False, error=str(exc), done_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise


def _load_meta() -> dict:
    try:
        with open(_FUND_META_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"periods": []}


def _save_meta(meta: dict) -> None:
    tmp = _FUND_META_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, _FUND_META_FILE)


def _load_existing_records() -> list[dict]:
    try:
        df = pd.read_csv(_FUND_FILE, dtype={"code": str})
        return df.to_dict("records")
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------------------
# 查询（PIT 对齐）
# ---------------------------------------------------------------------------

_pit_cache: pd.DataFrame | None = None
_pit_cache_version: str | None = None


def pit_table() -> pd.DataFrame:
    """财务长表（code,report_date,notice_date,指标）。构建后常驻内存。"""
    global _pit_cache, _pit_cache_version
    version = factor_data.data_version()
    if _pit_cache is None or _pit_cache_version != version:
        if not os.path.exists(_FUND_FILE):
            raise FileNotFoundError("PIT 财务数据未构建：先调 POST /api/factor/build（含财务阶段）")
        df = pd.read_csv(_FUND_FILE, dtype={"code": str})
        _pit_cache = df
        _pit_cache_version = version
    return _pit_cache


def as_of_panel(trade_dates: list[str]) -> pd.DataFrame:
    """PIT 对齐：index=trade_date(字符串日期), columns=(code, field) 的指标宽表。

    对每个交易日 T，取每只股票 NOTICE_DATE ≤ T 的最新 report_date 记录。
    实现按 (code,notice_date) 排序后 asof 合并——每个交易日只看得到已公告的。
    """
    fund = pit_table()
    fund = fund.sort_values(["code", "notice_date"])
    # 同一股票同一公告日多期（罕见）：留最新报告期
    fund = fund.drop_duplicates(subset=["code", "notice_date"], keep="last")

    dates_df = pd.DataFrame({"d": pd.DatetimeIndex(trade_dates)})
    out: dict[str, pd.DataFrame] = {}
    for code, g in fund.groupby("code"):
        g = g.set_index(pd.DatetimeIndex(g["notice_date"]))
        merged = pd.merge_asof(dates_df, g.drop(columns=["code", "notice_date"]),
                               left_on="d", right_index=True, direction="backward")
        merged.index = trade_dates  # 保持字符串日期索引，与日线宽表一致
        out[code] = merged
    if not out:
        return pd.DataFrame(index=trade_dates)
    wide = pd.concat(out, axis=1)  # columns = (code, field)
    return wide


def pit_status() -> dict:
    meta = _load_meta()
    info = {}
    if os.path.exists(_FUND_FILE):
        try:
            df = pit_table()
            info = {"rows": int(len(df)), "stocks": int(df["code"].nunique()),
                    "report_dates": int(df["report_date"].nunique()),
                    "notice_date_min": str(df["notice_date"].min()),
                    "notice_date_max": str(df["notice_date"].max())}
        except Exception:  # noqa: BLE001 — 状态查询不抛
            pass
    state = pit_state()
    return {
        "has_data": bool(info),
        "built": info,
        "periods_fetched": len(meta["periods"]),
        "building": state["building"],
        "progress": {"done": state["periods_done"], "total": state["periods_total"],
                     "rows": state["rows"], "started_at": state["started_at"],
                     "done_at": state["done_at"], "error": state["error"]},
        "pit_note": "因子按 NOTICE_DATE ≤ T 的最新报告期取数（公告后才可见）",
        "biases": ["notice_date 是东财最新公告日，更正公告会覆盖（个别老记录可见偏晚，不会提前）",
                   "指标为业绩报表口径，非完整三表"],
    }
