"""市场全景指数历史：点位百分位与可获得的官方估值，不补造长期估值序列。"""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import astock

_PERIOD_DAYS = {1: 250, 3: 750, 5: 1250, 10: 2500}
_INDEXES = {
    "sh000001": ("1.000001", "上证指数", "000001"),
    "sz399001": ("0.399001", "深证成指", None),
    "sz399006": ("0.399006", "创业板指", None),
    "sh000300": ("1.000300", "沪深300", "000300"),
    "sh000688": ("1.000688", "科创50", "000688"),
    "bj899050": ("0.899050", "北证50", "899050"),
}


def _percentile(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(v <= values[-1] for v in values) / len(values) * 100, 1)


def _empty_valuation() -> dict:
    return {"pe_ttm": None, "pb": None, "percentile": None, "date": None, "coverage": None, "source": None, "percentiles": {}}


def _valuation(code: str | None) -> dict:
    if not code:
        return _empty_valuation()
    try:
        response = requests.get(
            "https://www.csindex.com.cn/csindex-home/perf/index-perf",
            params={"indexCode": code, "startDate": "20100101", "endDate": datetime.now().strftime("%Y%m%d")},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.csindex.com.cn/"},
            timeout=25,
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
        points = [(row["tradeDate"], float(row["peg"])) for row in rows if row.get("peg") is not None and float(row["peg"]) > 0]
        if not points:
            return _empty_valuation()
        percentiles = {}
        for year, count in _PERIOD_DAYS.items():
            sample = points[-count:]
            percentiles[str(year)] = {"percentile": _percentile([v for _, v in sample]), "observations": len(sample), "start": sample[0][0]}
        return {
            "pe_ttm": points[-1][1], "pb": None, "percentile": percentiles["10"]["percentile"],
            "percentiles": percentiles, "date": points[-1][0],
            "coverage": f"{points[0][0]} 起 {len(points)} 个日度 PE 观测",
            "source": "中证指数官网·指数 PE",
        }
    except Exception:
        return _empty_valuation()


def _danjuan_valuation() -> dict[str, dict]:
    try:
        response = requests.get("https://danjuanfunds.com/djapi/index_eva/dj", headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        response.raise_for_status()
        rows = response.json().get("data", {}).get("items", [])
        result = {}
        for symbol in ("SZ399001", "SZ399006"):
            row = next((item for item in rows if item.get("index_code") == symbol), None)
            if not row:
                result[symbol] = _empty_valuation()
                continue
            history_file = Path(__file__).parent / "data" / f"{symbol.lower()}-valuation.json"
            points = json.loads(history_file.read_text()) if history_file.exists() else []
            current = {"date": datetime.fromtimestamp(row["ts"] / 1000, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d"), "pe": row.get("pe"), "pb": row.get("pb")}
            if current["pe"] and (not points or current["date"] > points[-1]["date"]):
                points.append(current)
                history_file.write_text(json.dumps(points, ensure_ascii=False, separators=(",", ":")))
            percentiles = {}
            for year, count in _PERIOD_DAYS.items():
                sample = points[-count:]
                if sample:
                    percentiles[str(year)] = {"percentile": _percentile([p["pe"] for p in sample]), "observations": len(sample), "start": sample[0]["date"]}
            result[symbol] = {
                "pe_ttm": row.get("pe") or None, "pb": row.get("pb") or None,
                "percentile": round(row["pe_percentile"] * 100, 1) if row.get("pe_percentile") is not None else None,
                "percentiles": percentiles, "date": current["date"],
                "coverage": f"{points[0]['date']} 起 {len(points)} 个日度观测；5/10年为可得历史，不足对应年限" if points else "历史暂不可用",
                "source": "蛋卷基金·公开估值与 DJEVA 历史备份（与官方 PE 口径不同）",
            }
        return result
    except Exception:
        return {symbol: _empty_valuation() for symbol in ("SZ399001", "SZ399006")}


def index_history(symbol: str) -> dict:
    if symbol not in _INDEXES:
        raise ValueError("不支持的指数")
    secid, name, valuation_code = _INDEXES[symbol]
    rows = astock.index_daily_em(secid, 2500)
    danjuan = _danjuan_valuation() if valuation_code is None else {}
    if not rows:
        raise RuntimeError("指数历史数据暂不可用")
    periods = {}
    for year, count in _PERIOD_DAYS.items():
        sample = rows[-count:]
        closes = [float(row["close"]) for row in sample]
        periods[str(year)] = {
            "years": year,
            "observations": len(sample),
            "start": sample[0]["date"],
            "end": sample[-1]["date"],
            "percentile": _percentile(closes),
            "history": [{"date": row["date"], "v": row["close"]} for row in sample],
        }
    return {
        "symbol": symbol,
        "name": name,
        "periods": periods,
        "valuation": _valuation(valuation_code) if valuation_code else danjuan[symbol.upper()],
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }
