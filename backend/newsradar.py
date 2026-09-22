"""资讯雷达数据层 —— 移植自 investment-news。

抓 12 赛道 108 个公开 RSS 源 → 合规过滤（赌/预测市场/加密/色情）+ 最近 N 天
+ 按赛道分组、时间倒序。纯标准库 + 线程池，零 key、零个股字段。

AI「今日要点」不在此模块——复用 Market Workbench 的可插拔 AI 层（前端调 /api/chat，
把某赛道资讯打包给用户自己的模型提炼）。本模块只出客观资讯。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import cache_runtime

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(HERE, "news_sources.json")
DATA_DIR = os.environ.get("MW_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".market-workbench")
CACHE_DIR = DATA_DIR
CACHE_FILE = os.path.join(CACHE_DIR, "radar.json")
LEGACY_CACHE_FILE = os.path.join(HERE, ".cache", "radar.json")
TTL = 2 * 3600

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BEIJING = timezone(timedelta(hours=8))

# ——— 条目层去重（同步自上游 investment-news v1.0.3 的 fetch.py）———
# 只剥公认的跟踪参数（白名单式保守剥）：有些站点用 query 区分文章 id，
# 剥多了会把两篇不同文章归一化成同一个 key、被去重丢掉一篇——那正是
# "去重做过头＝静默丢内容"。宁可少剥几个参数、偶尔重复显示一条，也不要丢文章。
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "spm", "share_token",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
}
# 同一条新闻被多家转载时，各家发布时间相近。标题去重限定在这个窗口内，
# 「每周综述」这类跨周复用的固定栏目名就不会被误判成重复。
_DUP_TITLE_WINDOW_S = 48 * 3600


def _normalize_url(url: str) -> str:
    """URL 归一化：剥跟踪参数 + 去锚点 + 去尾斜杠。解析失败退回原串小写，绝不抛——
    一条畸形链接不该中断整次刷新。"""
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    # scheme/host 大小写不敏感，path 保持原样（部分服务器 path 区分大小写）。
    # query 必须用 urlencode 重新转义：parse_qsl 已解码，手工拼接会让
    # `?id=a%26b%3Dc` 与 `?id=a&b=c` 归一化成同一个 key、误合并两篇不同文章。
    return urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(),
        parts.path.rstrip("/") or "/",
        urlencode(kept), ""))


def _normalize_title(title: str) -> str:
    """标题归一化：去空白、标点、大小写差异，用来识别转载。"""
    return re.sub(r"[\W_]+", "", (title or "").lower())


def _dedup(items: list[dict]) -> list[dict]:
    """同栏内去重：同一 URL 只留一条；标题相同且发布时间相近的也只留一条。

    传入的 items 需已按时间倒序，保留的即每组里最新的那条。
    任一方没有发布时间（ts=0）就不做标题去重：无从判断是转载还是同名栏目的
    不同期，判错的代价不对等——多显示一条只是冗余，判错删掉就是永久丢一篇。
    """
    seen_urls: set[str] = set()
    seen_titles: dict[str, int] = {}
    out = []
    for it in items:
        url_key = _normalize_url(it.get("url", ""))
        title_key = _normalize_title(it.get("title", ""))
        ts = it.get("ts", 0)

        dup_by_url = bool(url_key) and url_key in seen_urls
        prev_ts = seen_titles.get(title_key) if title_key else None
        dup_by_title = (prev_ts is not None and bool(ts) and bool(prev_ts)
                        and abs(prev_ts - ts) <= _DUP_TITLE_WINDOW_S)

        # 被丢弃的条目也要登记它的两个 key，否则重复关系传递不下去：
        # (标题A, url1) → (标题A, url2) → (标题B, url2) 三条同一新闻会漏成两张卡。
        if url_key:
            seen_urls.add(url_key)

        if dup_by_url or dup_by_title:
            if title_key and ts:
                seen_titles.setdefault(title_key, ts)
            continue
        # 保留的条目要**刷新**标题基准时间（不能 setdefault）：倒序遍历下，
        # 同名栏目在窗口外合法复现后，若基准仍停在最新那期，更旧的转载会因
        # 与最新期时间差超窗而逃过去重——基准必须跟着「最近保留的那条」走。
        if title_key and ts:
            seen_titles[title_key] = ts
        out.append(it)
    return out


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _parse_dt(s: str):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fetch_source(src: dict, per: int, cutoff, redline: list[str]):
    """抓单个 RSS 源；返回 items 列表，出错返回 None。"""
    try:
        req = urllib.request.Request(src["url"], headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*",
        })
        with urllib.request.urlopen(req, timeout=14) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        out = []
        for n in [e for e in root.iter() if _local(e.tag) in ("item", "entry")]:
            if len(out) >= per:
                break
            d = {"title": "", "url": "", "time": "", "ts": 0, "summary": "", "source": src["name"]}
            rawtime = ""
            for c in n:
                t = _local(c.tag)
                if t == "title" and not d["title"]:
                    d["title"] = (c.text or "").strip()
                elif t == "link" and not d["url"]:
                    d["url"] = c.get("href") or (c.text or "").strip()
                elif t in ("pubDate", "published", "updated", "date") and not rawtime:
                    rawtime = (c.text or "").strip()
                elif t in ("description", "summary", "content") and not d["summary"]:
                    d["summary"] = _strip_html(c.text or "")[:160]
            if not d["title"]:
                continue
            blob = (d["title"] + " " + d["summary"]).lower()
            if any(k in blob for k in redline):  # 合规红线过滤
                continue
            dt = _parse_dt(rawtime)
            if dt is not None:
                if cutoff and dt < cutoff:
                    continue
                d["time"] = dt.astimezone(BEIJING).strftime("%m-%d %H:%M")
                d["ts"] = int(dt.timestamp())
            else:
                d["time"] = "—"
            out.append(d)
        return out
    except Exception:
        return None


def fetch_radar() -> dict:
    """抓全部源，返回 12 赛道数据并落盘缓存。"""
    cfg = json.load(open(SOURCES_FILE, encoding="utf-8"))
    days = cfg.get("fetch", {}).get("recent_days", 7)
    per = cfg.get("fetch", {}).get("per_source", 6)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    redline = [k.lower() for k in cfg.get("redline_keywords", [])]

    byhint: dict[str, list] = {}
    for s in cfg["sources"]:
        byhint.setdefault(s["hint"], []).append(s)

    industries, tasks = [], []
    for i, ind in enumerate(cfg["industries"]):
        pool = byhint.get(ind["key"], [])
        industries.append({"key": ind["key"], "name": ind["name"], "accent": ind["accent"], "total": len(pool), "items": []})
        for s in pool:
            tasks.append((i, s))

    with ThreadPoolExecutor(max_workers=40) as ex:
        results = list(ex.map(lambda t: (t[0], _fetch_source(t[1], per, cutoff, redline)), tasks))

    previous = load_cache() or {}
    previous_by_source = {
        item.get("source"): []
        for industry in previous.get("industries", []) for item in industry.get("items", [])
        if item.get("source")
    }
    for industry in previous.get("industries", []):
        for item in industry.get("items", []):
            if item.get("source"):
                previous_by_source.setdefault(item["source"], []).append(item)
    failed = 0
    stale_sources = []
    for (idx, src), (_, items) in zip(tasks, results):
        if items is None:
            failed += 1
            stale_sources.append(src["name"])
            industries[idx]["items"].extend(previous_by_source.get(src["name"], []))
            continue
        industries[idx]["items"].extend(items)
    for ind in industries:
        ind["items"].sort(key=lambda x: x.get("ts", 0), reverse=True)
        ind["items"] = _dedup(ind["items"])  # 不同源转载同一条新闻只留最新一条

    data = {
        "generated_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        "recent_days": days,
        "industries": industries,
        "stats": {"industries": len(cfg["industries"]), "total_sources": len(cfg["sources"]),
                  "failed_sources": failed, "stale_sources": stale_sources},
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)  # 原子改名，防两次并发刷新交错写坏缓存
    return data


def load_cache():
    for path in (CACHE_FILE, LEGACY_CACHE_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return None


def skeleton() -> dict:
    """无缓存时返回赛道骨架（空 items），前端提示点刷新。"""
    cfg = json.load(open(SOURCES_FILE, encoding="utf-8"))
    byhint: dict[str, int] = {}
    for s in cfg["sources"]:
        byhint[s["hint"]] = byhint.get(s["hint"], 0) + 1
    return {
        "generated_at": None,
        "recent_days": cfg.get("fetch", {}).get("recent_days", 7),
        "industries": [{"key": i["key"], "name": i["name"], "accent": i["accent"], "total": byhint.get(i["key"], 0), "items": []} for i in cfg["industries"]],
        "stats": {"industries": len(cfg["industries"]), "total_sources": len(cfg["sources"])},
    }


def get_radar(force: bool = False) -> dict:
    value = cache_runtime.get(
        "rss_radar:v2", fetch_radar,
        valid=lambda data: bool(data.get("industries")), ttl=TTL,
        warm=load_cache, force=force,
    )
    return value if value.get("industries") else skeleton()


def start_scheduler() -> None:
    def loop():
        while True:
            time.sleep(TTL)
            get_radar(force=True)
    threading.Thread(target=loop, daemon=True, name="rss-radar-scheduler").start()
