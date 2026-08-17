"""产业链纵深聚合层。

链结构（图谱/瓶颈/传导）来自 backend/data/industry_chains.json 人工维护快照；
利润分布按链上代表公司批量抓财务摘要（同花顺源，单只可并发），每日缓存落盘。
聚合端点各块独立降级，结构与瓶颈不因财务或研报失败而阻塞。
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import astock
import cache_runtime

BEIJING = timezone(timedelta(hours=8))
DATA_DIR = os.environ.get("VR_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".vibe-research")
_CHAIN_FILE = os.path.join(os.path.dirname(__file__), "data", "industry_chains.json")
_FIN_CACHE_PRIMARY = os.path.join(DATA_DIR, "industry_chain_financials.json")
_FIN_CACHE_FALLBACK = os.path.join(os.path.dirname(__file__), ".cache", "industry_chain_financials.json")
_FIN_WORKERS = 8  # 同花顺财务摘要并发上限（保守，避免触发限流）
_TTL = 12 * 60 * 60  # 财务摘要按报告期变化，半天刷新一次足够
_SCHEMA_VERSION = 1
_PROFIT_METRICS = ("gross_margin", "net_margin", "roe", "revenue_yoy")


def _load_chains() -> dict:
    with open(_CHAIN_FILE, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("chains") or {}


def _chain_version() -> str:
    try:
        with open(_CHAIN_FILE, encoding="utf-8") as handle:
            return str(json.load(handle).get("version") or "unknown")
    except (OSError, json.JSONDecodeError):
        return "unknown"


def list_chains() -> dict:
    """链目录（纯静态）：供前端判断某板块是否已梳理产业链。"""
    chains = _load_chains()
    catalog = []
    for key, chain in chains.items():
        companies = {c["code"] for node in chain.get("nodes", []) for c in node.get("companies", [])}
        catalog.append({
            "key": key,
            "sector_key": chain.get("sector_key") or key,
            "label": chain.get("label", key),
            "length": chain.get("length"),
            "node_count": len(chain.get("nodes", [])),
            "company_count": len(companies),
            "bottleneck_count": len(chain.get("bottlenecks", [])),
        })
    return {"version": _chain_version(), "chains": catalog}


def chain_for_sector(sector_key: str) -> dict | None:
    """按板块 key 找链（sector_key 与链 key 不同名时也能命中）。"""
    chains = _load_chains()
    for key, chain in chains.items():
        if key == sector_key or chain.get("sector_key") == sector_key:
            return chain
    return None


# ---------------------------------------------------------------------------
# 利润分布：批量财务摘要 + 每日缓存（失败保留旧值并标 stale）
# ---------------------------------------------------------------------------

def _load_fin_cache() -> dict:
    for path in (_FIN_CACHE_PRIMARY, _FIN_CACHE_FALLBACK):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload.get("stocks"), dict):
                return payload
        except (OSError, json.JSONDecodeError):
            continue
    return {"updated_at": None, "stocks": {}}


def _save_fin_cache(cache: dict) -> None:
    payload = {
        "updated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "stocks": cache,
    }
    for path in (_FIN_CACHE_PRIMARY, _FIN_CACHE_FALLBACK):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            continue


def _num(value) -> float | None:
    """财务摘要字段常为 '18.05%' 这类带百分号的字符串，先剥掉再转。"""
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _fetch_financials(codes: list[str], force: bool = False) -> dict[str, dict]:
    """12 小时内直接用缓存；强刷绕过 TTL，失败保留旧值。"""
    today = datetime.now(BEIJING).date().isoformat()
    cache = _load_fin_cache()["stocks"]
    now = time.time()

    def fresh(row: dict) -> bool:
        try:
            return now - datetime.fromisoformat(str(row.get("fetched_at"))).timestamp() < _TTL
        except (TypeError, ValueError):
            return False

    missing = [code for code in codes if force or not fresh(cache.get(code) or {})]

    def fetch(code: str) -> tuple[str, dict]:
        summary = astock.financials(code) or {}
        return code, {
            "fetched_on": today,
            "fetched_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
            "period": summary.get("period"),
            "revenue_yoy": _num(summary.get("revenue_yoy")),
            "net_profit_yoy": _num(summary.get("net_profit_yoy")),
            "roe": _num(summary.get("roe")),
            "gross_margin": _num(summary.get("gross_margin")),
            "net_margin": _num(summary.get("net_margin")),
            "stale": False,
        }

    if missing:
        with ThreadPoolExecutor(max_workers=_FIN_WORKERS) as executor:
            futures = {executor.submit(fetch, code): code for code in missing}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    _, cache[code] = future.result()
                except Exception:  # noqa: BLE001 - 单只失败不阻塞其余
                    if code in cache:
                        cache[code] = {**cache[code], "stale": True}
        _save_fin_cache(cache)
    return {code: cache.get(code, {}) for code in codes}


def _median(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 2) if clean else None


def _build_profit(chain: dict, force: bool = False) -> dict:
    nodes = chain.get("nodes", [])
    codes = [c["code"] for node in nodes for c in node.get("companies", [])]
    financials = _fetch_financials(codes, force=force) if codes else {}

    rows = []
    node_index = {node["id"]: node for node in nodes}
    for node in nodes:
        for company in node.get("companies", []):
            fin = financials.get(company["code"]) or {}
            rows.append({
                "code": company["code"],
                "name": company["name"],
                "node_id": node["id"],
                "node_name": node["name"],
                "stage": node.get("stage"),
                "period": fin.get("period"),
                "gross_margin": fin.get("gross_margin"),
                "net_margin": fin.get("net_margin"),
                "roe": fin.get("roe"),
                "revenue_yoy": fin.get("revenue_yoy"),
                "stale": bool(fin.get("stale")),
            })

    node_stats = []
    for node in nodes:
        node_rows = [r for r in rows if r["node_id"] == node["id"]]
        stats = {"node_id": node["id"], "node_name": node["name"], "stage": node.get("stage"),
                 "company_count": len(node_rows), "sample_count": 0}
        for metric in _PROFIT_METRICS:
            values = [r[metric] for r in node_rows if r[metric] is not None]
            stats[metric] = _median(values)
            stats["sample_count"] = max(stats["sample_count"], len(values))
        node_stats.append(stats)

    # 利润沉淀环节：中位数毛利率与 ROE 综合最高的环节（纯统计描述）
    best = None
    for stats in node_stats:
        if stats["sample_count"] == 0:
            continue
        score = sum(
            (stats[m] or 0) for m in ("gross_margin", "roe")
        )
        if best is None or score > best[1]:
            best = (stats, score)
    settled = None
    if best and best[1] > 0:
        settled = {
            "node_id": best[0]["node_id"],
            "node_name": best[0]["node_name"],
            "stage": best[0]["stage"],
            "gross_margin": best[0]["gross_margin"],
            "roe": best[0]["roe"],
            "note": "按各环节代表公司中位数毛利率与 ROE 合计最高判定，仅为统计描述，不构成投资建议",
        }

    periods = {r["period"] for r in rows if r.get("period")}
    return {
        "rows": rows,
        "node_stats": node_stats,
        "settled_node": settled,
        "periods": sorted(periods),
        "stale_count": sum(1 for r in rows if r["stale"]),
        "source": "同花顺财务摘要（最新报告期，按日落盘缓存）",
    }


def _build_reports(chain: dict) -> dict:
    keywords = chain.get("report_keywords") or []
    rows = astock.eastmoney_industry_reports(keywords=keywords, days=90, max_pages=2)
    picked = []
    for row in rows[:10]:
        picked.append({
            "title": row.get("title"),
            "date": (row.get("publishDate") or "")[:10],
            "org": row.get("orgSName"),
            "industry": row.get("industryName"),
            "info_code": row.get("infoCode"),
        })
    return {"count": len(picked), "rows": picked, "source": "东方财富行业研报（近 90 天，按关键词过滤）"}


def _slim_structure(chain: dict) -> dict:
    return {
        "key": chain.get("key"),
        "sector_key": chain.get("sector_key"),
        "label": chain.get("label"),
        "length": chain.get("length"),
        "summary": chain.get("summary"),
        "nodes": chain.get("nodes", []),
        "links": chain.get("links", []),
        "bottlenecks": chain.get("bottlenecks", []),
        "transmission": chain.get("transmission") or {},
    }


def _build_chain(chain: dict, force: bool = False) -> dict:
    previous = _load_chain_cache(chain) or {}
    profit, reports = {}, {}
    try:
        profit = _build_profit(chain, force=force)
    except Exception as exc:  # noqa: BLE001 - 财务失败不阻塞结构展示
        profit = {**(previous.get("profit") or {}), "stale": True, "refresh_error": str(exc)}
    try:
        reports = _build_reports(chain)
    except Exception as exc:  # noqa: BLE001
        reports = {**(previous.get("reports") or {}), "stale": True, "refresh_error": str(exc)}
    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "chain_version": _chain_version(),
        "structure": _slim_structure(chain),
        "profit": profit,
        "reports": reports,
    }


def _chain_cache_files(chain: dict) -> tuple[str, str]:
    key = chain.get("key") or chain.get("sector_key") or "chain"
    primary = os.path.join(DATA_DIR, f"industry_chain_{key}.json")
    fallback = os.path.join(os.path.dirname(__file__), ".cache", f"industry_chain_{key}.json")
    return primary, fallback


def _load_chain_cache(chain: dict) -> dict | None:
    for path in _chain_cache_files(chain):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("schema_version") == _SCHEMA_VERSION:
                return payload
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _save_chain_cache(chain: dict, data: dict) -> None:
    primary, fallback = _chain_cache_files(chain)
    for path in (primary, fallback):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            continue


def get_chain(key: str, force: bool = False) -> dict:
    chains = _load_chains()
    chain = chains.get(key)
    if chain is None:
        for chain_key, item in chains.items():
            if item.get("sector_key") == key:
                chain = item
                break
    if chain is None:
        raise KeyError(f"未收录产业链：{key}")

    def build() -> dict:
        return _build_chain(chain, force=force)

    return cache_runtime.get(
        f"industry_chain:{chain.get('key') or key}:v{_SCHEMA_VERSION}",
        build,
        valid=lambda value: bool(value.get("structure", {}).get("nodes")),
        ttl=_TTL,
        warm=lambda: _load_chain_cache(chain),
        save=lambda data: _save_chain_cache(chain, data),
        force=force,
    )
