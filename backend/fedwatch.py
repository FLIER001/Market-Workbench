"""美联储利率追踪（FedWatch）—— 权威信号 × 预测市场对比与边际变化。

三条数据线（端点全部实测可用）：
  1. 官方锚（Fed.gov / NY Fed）
     · FOMC 日历（当前页 + 未来年页链接）+ 最新声明（目标区间/决策句）
     · EFFR 与目标区间历史（NY Fed 官方 API）
  2. 权威信号 = 自算 CME FedWatch 等价物
     · ZQ（30 天联邦基金期货）月度合约价格 ← Yahoo query2（免费无 key）
     · 概率按 CME 官方方法论：
       - 当月会议已开完 → 用当月合约 + 已知 EFFR 历史反推市场隐含「新即期利率」
         （解决 NY Fed EFFR 滞后一期的问题；实测 9/16 加息后锚=3.876%，与
          CME 隐含锚 3.880% 一致）
       - 逐会议递推：P = (implied − 无行动路径) / (25bp × 生效天数占比)；
         无会议月合约做路径再锚定；路径更新用未截断期望
       - 验证（2026-09-17）：10月加息 56% vs CME 50.9%（早间价）；
         1月加息 31% vs Polymarket 31%
  3. 预测市场 = Polymarket gamma API
     · Fed Decision 事件 + 年内加息/降息次数 + 年底利率水平
     · 每个结果：YES 概率 + 1日/1周/1月价格变动 + bestBid/Ask（events 内嵌）

对比矩阵：同一会议 自算概率 vs Polymarket 概率 + 概率差 + 双边 24h 边际变化
（ZQ 侧靠本地快照历史 jsonl 计算 24h/7d 变化；PM 侧用官方 chg 字段）。
合规：只做客观数据整理，不预置观点、不建议。
"""

from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import cache_runtime

BEIJING = timezone(timedelta(hours=8))
ET = ZoneInfo("America/New_York")

_SNAPSHOT_DIR = os.environ.get("MW_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".market-workbench")
_SNAPSHOT = os.path.join(_SNAPSHOT_DIR, "fedwatch_snapshot.json")
_HISTORY = os.path.join(_SNAPSHOT_DIR, "fedwatch_history.jsonl")

_TTL_FAST = 120            # 概率快照 2 分钟（盘中边际变化敏感）
_HIST_MAX_LINES = 4000     # 历史线保留上限（10 分钟一条 ≈ 27 天）

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_YAHOO_CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d"
_NYFED_EFFR = ("https://markets.newyorkfed.org/api/rates/unsecured/effr/"
               "search.json?startDate={start}&endDate={end}")
_FOMC_CAL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_PM_GAMMA_EVENTS = "https://gamma-api.polymarket.com/events?tag_slug={slug}&closed=false&limit=30"
# CME 官方口径存档（第三方 GitHub Actions 每日爬 cmegroup.cn/fed-watch 存档；可能停更，仅交叉校验用）
_CME_ARCHIVE_CSV = ("https://raw.githubusercontent.com/zuowood1234/cme-fedwatch-tracker/"
                    "main/data/fedwatch_history.csv")

# ZQ 月度合约代码：F G H J K M N Q U V X Z（1-12 月）
_ZQ_MONTH_CODES = "FGHJKMNQUVXZ"
_MONTH_NUM = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
_MONTH_NUM.update({m[:3]: i + 1 for m, i in
                   [(k, v - 1) for k, v in _MONTH_NUM.items()]})


_PROXY_CANDIDATES = (
    "http://127.0.0.1:7890",   # Clash 默认
    "http://127.0.0.1:7897",   # Clash Verge
)
_PROXY: str | None = None      # 进程内 latch：探测一次后复用


def _proxy_env() -> str | None:
    """系统代理（https_proxy/HTTPS_PROXY/ALL_PROXY），有就用。"""
    for k in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _detect_proxy() -> str | None:
    """探测可用代理：系统 env 优先，然后本地常见 Clash 端口，直连兜底 None。"""
    global _PROXY
    if _PROXY is not None:
        return _PROXY
    cand = [_proxy_env()] + [p for p in _PROXY_CANDIDATES]
    for p in cand:
        if not p:
            continue
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "4", "-x", p,
                 "https://gamma-api.polymarket.com/events?limit=1"],
                capture_output=True, timeout=6)
            if r.returncode == 0 and r.stdout.startswith(b"["):
                _PROXY = p
                return p
        except Exception:  # noqa: BLE001 — 探测失败试下一个
            continue
    _PROXY = ""  # 直连（空串 = 不用代理）
    return None


_YAHOO_COOKIES = {
    "query1": None, "query2": None,   # token_id → Cookie 头（限流后按需刷新）
}


def _fetch(url: str, timeout: int = 20) -> str:
    """HTTP GET（text）。通道：urllib 直连（TUN 接管，常稳）→ 代理 curl 兜底。"""
    global _PROXY
    # 通道 1：urllib 直连（本机 TUN 模式下浏览器级直连稳定）
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            if body:
                return body
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError(f"Yahoo 限流 429 url={url[:80]}")
        # 其他 HTTP 错误 → 试 curl 通道
    except Exception:  # noqa: BLE001 — 网络层失败 → curl 通道
        pass
    # 通道 2：curl（探测过的本地代理）
    if _PROXY is None:
        _detect_proxy()
    cmd = ["curl", "-L", "-s", "--max-time", str(timeout), "-A", _UA]
    if _PROXY:
        cmd += ["-x", _PROXY]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    if r.returncode != 0 or not r.stdout:
        # 代理探测结果可能过期（Clash 重启）→ 清 latch 直连重试一次
        if _PROXY:
            _PROXY = None
            cmd2 = ["curl", "-L", "-s", "--max-time", str(timeout), "-A", _UA, url]
            r = subprocess.run(cmd2, capture_output=True, timeout=timeout + 5)
            if r.returncode != 0 or not r.stdout:
                raise RuntimeError(f"fetch 失败 rc={r.returncode} url={url[:80]}")
        else:
            raise RuntimeError(f"fetch 失败 rc={r.returncode} url={url[:80]}")
    return r.stdout.decode("utf-8", "replace")


def _fetch_json(url: str, timeout: int = 20):
    return json.loads(_fetch(url, timeout=timeout))


def _month_key(y: int, m: int) -> str:
    return f"{y}-{m:02d}"


def _next_month(key: str) -> str:
    y, m = int(key[:4]), int(key[5:7])
    return _month_key(y + (m // 12), (m % 12) + 1)


# ---------------------------------------------------------------------------
# 1) 官方锚：FOMC 日历 + 最新声明 + EFFR 历史
# ---------------------------------------------------------------------------

def _parse_fomc_calendar(html: str) -> list[dict]:
    """解析 FOMC 日历页（含年份分段的任意一年页）→ 会议列表。

    页面结构：月份和日期分在不同 div（<strong>October</strong>…<div>27-28</div>），
    直接对原始 html 跑正则会全部落空——先去标签、压缩空白再解析。
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    out: list[dict] = []
    for year, seg in re.findall(
            r"(20\d{2}) FOMC Meetings(.+?)(?=\d{4} FOMC Meetings|Future Year|$)",
            text, re.S):
        for mon, d1, d2 in re.findall(
                r"(January|February|March|April|May|June|July|August|September|"
                r"October|November|December)\s+(\d{1,2})-(\d{1,2})", seg):
            y, m = int(year), _MONTH_NUM[mon]
            out.append({
                "label": _month_key(y, m),
                "start": datetime(y, m, int(d1), tzinfo=ET),
                "end": datetime(y, m, int(d2), tzinfo=ET),
                "has_sep": m in (3, 6, 9, 12),
                "source": "fedgov",
            })
    seen, uniq = set(), []
    for m in out:
        if m["label"] not in seen:
            seen.add(m["label"])
            uniq.append(m)
    return uniq


def _all_meetings() -> list[dict]:
    """当前日历页 + 「Future Year」链接页合并，升序去重。"""
    html = _fetch(_FOMC_CAL, timeout=15)
    meetings = _parse_fomc_calendar(html)
    # 未来年页（如 2027）：从页面链接发现，最多追两层
    seen_urls = {_FOMC_CAL}
    for href in re.findall(r'href="([^"]*fomccalendar[^"]*)"', html):
        url = href if href.startswith("http") else (
            "https://www.federalreserve.gov" + href)
        url = url.split("#")[0]
        if url in seen_urls or "fomccalendars.htm" in url:
            continue
        seen_urls.add(url)
        try:
            meetings += _parse_fomc_calendar(_fetch(url, timeout=15))
        except Exception:  # noqa: BLE001 — 未来年页失败不阻塞
            continue
    uniq: dict[str, dict] = {}
    for m in meetings:
        uniq.setdefault(m["label"], m)
    return sorted(uniq.values(), key=lambda m: m["start"])


def _frac_to_float(s: str) -> float | None:
    """'3-3/4' / '3/4' / '4' → 3.75 / 0.75 / 4.0（Fed 声明用分数写法）。"""
    s = s.strip()
    m = re.fullmatch(r"(\d+)-(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.fullmatch(r"(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) / int(m.group(2))
    try:
        return float(s)
    except ValueError:
        return None


def _latest_statement(cal_html: str) -> dict:
    """最新 FOMC 声明：日期/URL/动作/目标区间/决策句。"""
    links = sorted(set(re.findall(r"monetary(20\d{6})a\.htm", cal_html)))
    if not links:
        return {}
    latest = links[-1]
    y, m, d = int(latest[:4]), int(latest[4:6]), int(latest[6:8])
    url = (f"https://www.federalreserve.gov/newsevents/pressreleases/"
           f"monetary{latest}a.htm")
    out: dict = {"date": f"{y}-{m:02d}-{d:02d}", "url": url}
    try:
        body = _fetch(url, timeout=15)
    except Exception:  # noqa: BLE001 — 声明正文失败不影响日期/链接
        return out
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    text = (text.replace("\u00bc", " 1/4 ").replace("\u00bd", " 1/2 ")
                .replace("\u00be", " 3/4 "))
    m = re.search(
        r"(?:decided|voted)[^.]*?to (raise|lower|maintain)[^.]*?target range"
        r"[^.]*?to\s+([^.]*?)percent", text)
    if m:
        out["action"] = m.group(1)
        # 段形如 '3-3/4 to 4 '：token 化后取首尾（'X-Y/Z' 分数写法 → _frac_to_float）
        toks = re.findall(r"\d+-\d+/\d+|\d+/\d+|\d+", m.group(2))
        nums = [v for v in (_frac_to_float(t) for t in toks) if v is not None]
        # 过滤步长噪声（'by 1/4 percentage point' 已被正则排除在 group(2) 外；
        # 若仍混入 0.25/1/4 等小值则丢弃，保留 ≥0.5 的目标区间端点）
        nums = [v for v in nums if v >= 0.5]
        if len(nums) >= 2:
            out["target_low"], out["target_high"] = nums[0], nums[-1]
        out["sentence"] = m.group(0).strip()[:280]
    return out


def _effr_history(days: int = 50) -> list[dict]:
    """NY Fed EFFR 日序列（升序）：[{date, rate, target_low, target_high}]。"""
    end = datetime.now(ET).date()
    j = _fetch_json(_NYFED_EFFR.format(start=end - timedelta(days=days), end=end))
    refs = sorted(j.get("refRates") or [], key=lambda r: r.get("effectiveDate") or "")
    out = []
    for r in refs:
        if r.get("percentRate") is None:
            continue
        out.append({
            "date": r.get("effectiveDate"),
            "rate": float(r["percentRate"]),
            "target_low": r.get("targetRateFrom"),
            "target_high": r.get("targetRateTo"),
        })
    return out


def _official_block() -> dict:
    """FOMC 日历（未来 4 次会议）+ 最新声明 + EFFR 历史。"""
    meetings = _all_meetings()
    cal_html = _fetch(_FOMC_CAL, timeout=15)
    now = datetime.now(ET)
    upcoming = sorted((m for m in meetings if m["end"] > now),
                      key=lambda m: m["start"])
    next_m = upcoming[0] if upcoming else None
    stmt = _latest_statement(cal_html)
    effr_hist = _effr_history()
    effr = effr_hist[-1] if effr_hist else None
    return {
        "next_meeting": {
            "label": next_m["label"],
            "dates": f"{next_m['start']:%Y-%m-%d} ~ {next_m['end']:%Y-%m-%d}",
            "days_away": round((next_m["start"] - now).total_seconds() / 86400, 1),
        } if next_m else None,
        "meetings": [{
            "label": m["label"],
            "dates": f"{m['start']:%Y-%m-%d} ~ {m['end']:%Y-%m-%d}",
            "has_sep": m["has_sep"],
        } for m in upcoming[:4]],
        "latest_statement": stmt,
        "effr": effr,
        "fetched_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 2) 权威信号：ZQ 期货 → 自算 FedWatch 概率（CME 官方方法论）
# ---------------------------------------------------------------------------

def _zq_symbol(year: int, month: int) -> str:
    return f"ZQ{_ZQ_MONTH_CODES[month - 1]}{str(year)[-2:]}.CBT"


def _fetch_zq_quotes() -> dict[str, dict]:
    """当月起 13 个月 ZQ 合约现价。返回 {YYYY-MM: {price, symbol, market_time}}。

    Yahoo 限流：多合约时被 429 概率高。策略：慢速（1.2s 间隔）+ 429 指数退避，
    并允许「部分成功」——只要锚月 + 至少 2 个会议月合约在，概率链就能算。
    """
    today = datetime.now(ET)
    out: dict[str, dict] = {}
    # Yahoo 429 全局冷却：IP 级限流时连打 13 个请求只会加剧封锁。
    # 冷却 10 分钟内直接放弃（last-good 兜底），到期后重试一个合约探路。
    global _ZQ_COOLDOWN_UNTIL
    if _ZQ_COOLDOWN_UNTIL and time.time() < _ZQ_COOLDOWN_UNTIL:
        return out
    symbols = []
    for i in range(13):
        y = today.year + (today.month - 1 + i) // 12
        m = (today.month - 1 + i) % 12 + 1
        symbols.append((_month_key(y, m), _zq_symbol(y, m)))
    # 先拉锚月（当月/无会议月）+ 4 个会议月，其余月合约在限流预算内尽力
    meeting_keys = {m["label"] for m in _all_meetings_cached()}
    priority = [s for s in symbols if s[0] == symbols[0][0] or s[0] not in meeting_keys]
    rest = [s for s in symbols if s not in priority]
    ordered = priority[:5] + rest
    rate_limited = 0
    for idx, (key, sym) in enumerate(ordered):
        for attempt in range(3):
            try:
                j = _fetch_json(_YAHOO_CHART.format(sym=sym.replace("=", "%3D")))
                meta = j["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                if price is None:
                    break
                out[key] = {
                    "price": float(price),
                    "symbol": sym,
                    "market_time": meta.get("regularMarketTime"),
                }
                break
            except RuntimeError as e:  # 429：立即冷却，放弃本轮剩余合约
                if "429" in str(e) or "限流" in str(e):
                    rate_limited += 1
                    _ZQ_COOLDOWN_UNTIL = time.time() + 600
                    return out
            except Exception:  # noqa: BLE001 — 退避后重试
                pass
            if attempt == 2:
                break
            time.sleep((2 ** attempt) * 2)  # 2s / 4s
        time.sleep(1.2)
    return out


_ZQ_COOLDOWN_UNTIL = 0.0


_ALL_MEETINGS_CACHE: list[dict] | None = None


def _all_meetings_cached() -> list[dict]:
    """会议列表进程内缓存（10 分钟），避免 ZQ 拉取时重复抓日历页。"""
    global _ALL_MEETINGS_CACHE
    if _ALL_MEETINGS_CACHE is None:
        _ALL_MEETINGS_CACHE = _all_meetings()
    return _ALL_MEETINGS_CACHE


def _spot_rate(zq: dict[str, dict], effr_hist: list[dict],
               meetings: list[dict], statement: dict) -> dict:
    """市场隐含的当前即期利率（无行动路径起点）。

    优先级：
    1) 当月会议已开完 → 当月合约 + 已知 EFFR 历史反推「会后新利率」
       （自校准，绕开 NY Fed EFFR 滞后一期的问题）
    2) 声明比 EFFR 观测新（刚加/降息）→ EFFR + 目标区间中值变动
    3) 直接用最新 EFFR
    """
    today = datetime.now(ET)
    today_key = _month_key(today.year, today.month)
    effr = effr_hist[-1] if effr_hist else None

    cur_meeting = next((m for m in meetings if m["label"] == today_key), None)
    if cur_meeting and cur_meeting["end"] < today and today_key in zq and effr:
        # 会后新利率反推：implied×N = Σ(会前各日EFFR) + r_new×会后天数
        y, mo = today.year, today.month
        n_days = calendar.monthrange(y, mo)[1]
        eff_day = min(cur_meeting["end"].day + 1, n_days)
        pre_days = eff_day - 1
        post_days = n_days - eff_day + 1
        if pre_days >= 1 and post_days >= 1:
            # 当月会前各日 EFFR（缺失日沿用最后已知值）
            hist = {r["date"]: r["rate"] for r in effr_hist}
            last_rate = effr["rate"]
            pre_sum = 0.0
            for d in range(1, pre_days + 1):
                last_rate = hist.get(f"{today_key}-{d:02d}", last_rate)
                pre_sum += last_rate
            implied = 100 - zq[today_key]["price"]
            r_new = (implied * n_days - pre_sum) / post_days
            if 0.0 < r_new < 15.0:
                return {"rate": round(r_new, 4), "method": "当月合约反推（会后新利率）",
                        "contract": zq[today_key]["symbol"]}

    if effr and statement.get("target_low") is not None and effr.get("target_low") is not None:
        try:
            stmt_date = datetime.strptime(statement["date"], "%Y-%m-%d").date()
            effr_date = datetime.strptime(effr["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            stmt_date = effr_date = None
        # 同日也触发：决策当日 14:00 ET 公布，EFFR 按当日（旧）目标区间计价，
        # 新利率次日才生效 → stmt_date == effr_date 时 EFFR 仍是旧锚
        if stmt_date and effr_date and stmt_date >= effr_date:
            delta = ((statement["target_low"] + statement["target_high"]) / 2
                     - (effr["target_low"] + effr["target_high"]) / 2)
            if delta:
                return {"rate": round(effr["rate"] + delta, 4),
                        "method": f"EFFR + 声明目标区间变动({delta:+.2f}pp)"}
    if effr:
        return {"rate": effr["rate"], "method": "最新 EFFR"}
    return {"rate": None, "method": "不可用"}


def _fedwatch_probs(zq: dict[str, dict], spot: dict,
                    meetings: list[dict]) -> list[dict]:
    """逐会议递推升降息概率（CME 方法论等价）。

    · 无会议月合约 = 路径水平的无偏锚 → 每次会议前对路径再锚定
    · 会议月：P = (implied − 无行动路径) / (25bp × 会后天数/当月天数)
    · 路径更新用未截断期望（截断值只用于展示）
    """
    rate = spot.get("rate")
    if rate is None or not zq or not meetings:
        return []
    today = datetime.now(ET)
    today_key = _month_key(today.year, today.month)
    meeting_keys = {m["label"] for m in meetings}

    out: list[dict] = []
    path = rate
    prev_key = today_key
    for mt in meetings:
        label = mt["label"]
        # 前一次锚定月与本次会议月之间的无会议月：用合约再锚定路径
        k = _next_month(prev_key)
        while k != label:
            if k not in meeting_keys and k in zq:
                path = 100 - zq[k]["price"]
            k = _next_month(k)
        q = zq.get(label)
        if not q:
            prev_key = label
            continue
        y, mo = int(label[:4]), int(label[5:7])
        n_days = calendar.monthrange(y, mo)[1]
        # 决策生效日 = 会议结束次日（会议第 2 天公布，次日生效）
        eff_day = min(mt["end"].day + 1, n_days)
        days_after = n_days - eff_day + 1
        implied = 100 - q["price"]
        step = 0.25 * days_after / n_days if days_after > 0 else 0.0
        raw = (implied - path) / step if step > 0 else 0.0
        p_hike = min(max(raw, 0.0), 1.0)
        p_cut = min(max(-raw, 0.0), 1.0)
        p_hold = max(0.0, 1.0 - p_hike - p_cut)
        out.append({
            "meeting": label,
            "dates": f"{mt['start']:%Y-%m-%d} ~ {mt['end']:%Y-%m-%d}",
            "implied_rate": round(implied, 4),
            "path_rate": round(path, 4),
            "p_hike": round(p_hike, 4),
            "p_cut": round(p_cut, 4),
            "p_hold": round(p_hold, 4),
            "contract": q["symbol"],
            "contract_price": q["price"],
        })
        path = path + raw * 0.25  # 未截断期望更新
        prev_key = label
    return out


# ---------------------------------------------------------------------------
# 3) 预测市场：Polymarket gamma
# ---------------------------------------------------------------------------

_PM_EVENT_RE = re.compile(
    r"^Fed Decision in \w+\??$"
    r"|^How many Fed rate (?:hikes|cuts) in \d{4}\?$"
    r"|^What will the Fed rate be at the end of \d{4}\?$", re.I)

_PM_ACTION_MAP = {
    "50+ bps decrease": "cut50", "25 bps decrease": "cut25",
    "No change": "hold", "25 bps increase": "hike25", "50+ bps increase": "hike50",
}


def _pm_events() -> list[dict]:
    """Polymarket Fed 相关事件（fomc+fed 两个 tag 并集，仅未平仓）。"""
    events: dict[str, dict] = {}
    for slug in ("fomc", "fed"):
        try:
            evs = _fetch_json(_PM_GAMMA_EVENTS.format(slug=slug))
        except Exception:  # noqa: BLE001 — 单 tag 失败跳过
            continue
        for ev in evs:
            t = (ev.get("title") or "").strip()
            if not _PM_EVENT_RE.match(t):
                continue
            if ev["id"] in events:
                continue
            markets = []
            for mk in ev.get("markets") or []:
                try:
                    prices = json.loads(mk.get("outcomePrices") or "[]")
                except Exception:  # noqa: BLE001
                    prices = []
                label = mk.get("groupItemTitle") or (mk.get("question") or "")[:80]
                if not label or not prices:
                    continue
                try:
                    yes = float(prices[0])
                except (TypeError, ValueError, IndexError):
                    yes = None
                markets.append({
                    "label": label,
                    "yes": yes,
                    "vol24h": round(mk.get("volume24hr") or 0),
                    "chg_1d": mk.get("oneDayPriceChange"),
                    "chg_1w": mk.get("oneWeekPriceChange"),
                    "chg_1m": mk.get("oneMonthPriceChange"),
                    "best_bid": mk.get("bestBid"),
                    "best_ask": mk.get("bestAsk"),
                    "market_id": mk.get("id"),
                })
            if not markets:
                continue
            events[ev["id"]] = {
                "title": t,
                "end": (ev.get("endDate") or "")[:10],
                "vol24h": round(ev.get("volume24hr") or 0),
                "markets": markets,
            }
    return sorted(events.values(), key=lambda e: e["end"] or "9999")


def _cme_archive_block() -> dict:
    """CME 官方口径存档（第三方 GitHub Actions 爬 cmegroup.cn 存档，可能停更）。

    CSV 为「利率区间分布」格式：当前区间行 = no_change，高于当前区间求和 = hike，
    低于当前求和 = ease（与 CME 官网 FedWatch 概率树同口径）。
    返回 {snapshot_date, meetings: {YYYY-MM-DD: {ease, no_change, hike, ...}}}。
    """
    import csv as _csv
    import io as _io
    try:
        raw = _fetch(_CME_ARCHIVE_CSV, timeout=20)
    except Exception:  # noqa: BLE001 — 存档不可达时静默缺省
        return {"error": "存档不可达"}
    rows = list(_csv.DictReader(_io.StringIO(raw)))
    if not rows:
        return {"error": "存档为空"}
    latest_date = max(r["snapshot_date"] for r in rows)
    # 两遍：第一遍找每个会议的「当前区间」下界（(Current) 标记行）
    cur_lo: dict[str, int] = {}
    for r in rows:
        if r["snapshot_date"] != latest_date:
            continue
        if "Current" in (r.get("rate_range") or ""):
            try:
                cur_lo[r["meeting_date"][:10]] = int(r["rate_range"].split("-")[0])
            except (ValueError, IndexError):
                continue
    meetings: dict[str, dict] = {}
    for r in rows:
        if r["snapshot_date"] != latest_date:
            continue
        md = (r.get("meeting_date") or "")[:10]
        rr = (r.get("rate_range") or "").replace(" (Current)", "")
        if not md or not rr or md not in cur_lo:
            continue
        try:
            prob = float(r["prob_now"])
            lo, hi = int(rr.split("-")[0]), int(rr.split("-")[-1])
        except (KeyError, ValueError):
            continue
        slot = meetings.setdefault(md, {
            "ease": 0.0, "no_change": 0.0, "hike": 0.0,
            "contract": r.get("contract") or "",
            "mid_price": r.get("mid_price") or "",
        })
        if "Current" in (r.get("rate_range") or ""):
            slot["no_change"] = prob
        elif lo >= cur_lo[md]:
            slot["hike"] += prob
        else:
            slot["ease"] += prob
    out = {k: {"ease": v["ease"], "no_change": v["no_change"], "hike": v["hike"],
               "contract": v["contract"], "mid_price": v["mid_price"]}
           for k, v in meetings.items()
           if k >= datetime.now(ET).strftime("%Y-%m-%d")}
    return {"snapshot_date": latest_date, "meetings": dict(sorted(out.items()))}


def _pm_block() -> dict:
    """Polymarket 层：决策事件（按会议月标注）+ 次数 + 年底水平。"""
    decisions, counts, level = [], [], []
    for ev in _pm_events():
        t = ev["title"]
        m = re.match(r"^Fed Decision in (\w+)\?", t, re.I)
        if m and m.group(1) in _MONTH_NUM:
            month = _MONTH_NUM[m.group(1)]
            ev2 = dict(ev)
            # 事件结束日 = 决策日（会议第 2 天）→ 会议月 = 决策日月
            try:
                end_y = int((ev.get("end") or "0")[:4])
            except ValueError:
                end_y = 0
            ev2["meeting"] = _month_key(end_y, month) if end_y else None
            decisions.append(ev2)
            continue
        m = re.match(r"^How many Fed rate (hikes|cuts) in (\d{4})\?", t, re.I)
        if m:
            ev2 = dict(ev)
            ev2["kind"], ev2["year"] = m.group(1).lower(), m.group(2)
            counts.append(ev2)
            continue
        if re.match(r"^What will the Fed rate be at the end of", t, re.I):
            level.append(ev)
    return {
        "decisions": decisions,
        "counts": sorted(counts, key=lambda e: e["vol24h"], reverse=True),
        "level": level,
    }


# ---------------------------------------------------------------------------
# 4) 快照历史（ZQ 侧边际变化：24h / 7d 概率变动）
# ---------------------------------------------------------------------------

def _hist_append(probs: list[dict], pm_decisions: list[dict]) -> None:
    """每次成功构建后追加一行（10 分钟调度 + 页面请求各触发一次）。"""
    if not probs:
        return
    line = {
        "ts": int(time.time()),
        "probs": {p["meeting"]: {"h": p["p_hike"], "c": p["p_cut"]}
                  for p in probs},
        "pm": {},
    }
    for dec in pm_decisions:
        if not dec.get("meeting"):
            continue
        h = c = 0.0
        for o in dec["markets"]:
            act = _PM_ACTION_MAP.get(o["label"])
            if act and o["yes"] is not None:
                if act.startswith("hike"):
                    h += o["yes"]
                elif act.startswith("cut"):
                    c += o["yes"]
        line["pm"][dec["meeting"]] = {"h": round(h, 4), "c": round(c, 4)}
    try:
        os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
        with open(_HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        # 裁剪：保留最近 _HIST_MAX_LINES 行
        try:
            with open(_HISTORY, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > _HIST_MAX_LINES:
                with open(_HISTORY, "w", encoding="utf-8") as f:
                    f.writelines(lines[-_HIST_MAX_LINES:])
        except OSError:
            pass
    except OSError:
        pass


def _hist_delta(meeting: str, kind: str, field: str,
                lookback_s: int, tol_s: int) -> float | None:
    """从历史 jsonl 里找距 now-lookback 最近（±tol）的一条，算字段差。"""
    if not os.path.exists(_HISTORY):
        return None
    try:
        with open(_HISTORY, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    target = time.time() - lookback_s
    best, best_dt = None, None
    for ln in lines[-2000:]:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        dt = abs(row.get("ts", 0) - target)
        if best_dt is None or dt < best_dt:
            best, best_dt = row, dt
    if best is None or best_dt is None or best_dt > tol_s:
        return None
    src = (best.get("probs") if kind == "zq" else best.get("pm")) or {}
    node = src.get(meeting) or {}
    return node.get(field)


def _marginal_changes(probs: list[dict], pm_decisions: list[dict]) -> dict:
    """双边 24h/7d 边际变化：ZQ 侧用本地历史，PM 侧用官方 chg 字段。"""
    out: dict = {"zq": {}, "pm": {}, "history_note": None}
    for p in probs:
        mt = p["meeting"]
        for field, key in (("h", "p_hike"), ("c", "p_cut")):
            for lb, tol, tag in ((86400, 6 * 3600, "chg_24h"),
                                 (7 * 86400, 2 * 86400, "chg_7d")):
                base = _hist_delta(mt, "zq", field, lb, tol)
                if base is not None:
                    out["zq"].setdefault(mt, {})[f"{key}_{tag}"] = round(
                        p[key] - base, 4)
    for dec in pm_decisions:
        mt = dec.get("meeting")
        if not mt:
            continue
        h_chg = c_chg = None
        hold_chg = None
        for o in dec["markets"]:
            act = _PM_ACTION_MAP.get(o["label"])
            ch = o.get("chg_1d")
            if act and ch is not None:
                if act.startswith("hike"):
                    h_chg = round((h_chg or 0) + ch, 4)
                elif act.startswith("cut"):
                    c_chg = round((c_chg or 0) + ch, 4)
                elif act == "hold":
                    hold_chg = ch
        out["pm"][mt] = {"p_hike_chg_24h": h_chg, "p_cut_chg_24h": c_chg,
                         "p_hold_chg_24h": hold_chg}
    if os.path.exists(_HISTORY):
        try:
            with open(_HISTORY, encoding="utf-8") as f:
                n = sum(1 for _ in f)
            out["history_note"] = f"本地快照历史 {n} 条；ZQ 侧 24h/7d 变化依赖历史覆盖"
        except OSError:
            pass
    else:
        out["history_note"] = "本地历史尚空：ZQ 侧 24h 变化将在积累后可用"
    return out


# ---------------------------------------------------------------------------
# 5) 对比矩阵 + 主构建
# ---------------------------------------------------------------------------

def _build_matrix(fut_probs: list[dict], pm_decisions: list[dict],
                  marginal: dict) -> list[dict]:
    """同一会议：自算概率 vs Polymarket 概率 + 概率差 + 边际变化。"""
    by_meeting = {d.get("meeting"): d for d in pm_decisions if d.get("meeting")}
    mq = marginal.get("zq") or {}
    mp = marginal.get("pm") or {}
    matrix = []
    for fp in fut_probs:
        mt = fp["meeting"]
        row = {
            "meeting": mt,
            "dates": fp["dates"],
            "implied_rate": fp["implied_rate"],
            "cme_equiv": {
                "p_hike": fp["p_hike"], "p_cut": fp["p_cut"], "p_hold": fp["p_hold"],
                "contract": fp["contract"], "contract_price": fp["contract_price"],
                "chg": mq.get(mt) or {},
            },
        }
        dec = by_meeting.get(mt)
        if dec:
            acts = {(_PM_ACTION_MAP.get(o["label"])): o
                    for o in dec["markets"] if _PM_ACTION_MAP.get(o["label"])}
            pm_hike = sum(o["yes"] for k, o in acts.items()
                          if k in ("hike25", "hike50") and o["yes"] is not None)
            pm_cut = sum(o["yes"] for k, o in acts.items()
                         if k in ("cut25", "cut50") and o["yes"] is not None)
            pm_hold = (acts.get("hold") or {}).get("yes")
            row["polymarket"] = {
                "p_hike": round(pm_hike, 4), "p_cut": round(pm_cut, 4),
                "p_hold": round(pm_hold, 4) if pm_hold is not None else None,
                "vol24h": dec["vol24h"],
                "outcomes": dec["markets"],
                "chg": mp.get(mt) or {},
            }
            row["diff"] = {
                "p_hike_diff": round(pm_hike - fp["p_hike"], 4),
                "p_cut_diff": round(pm_cut - fp["p_cut"], 4),
                "p_hold_diff": (round(pm_hold - fp["p_hold"], 4)
                                if pm_hold is not None else None),
            }
        else:
            row["polymarket"] = None
            row["diff"] = None
        matrix.append(row)
    return matrix


def _build_snapshot() -> dict:
    data: dict = {"schema_version": 1,
                  "updated": datetime.now(BEIJING).isoformat(timespec="seconds")}

    # 官方层
    try:
        official = _official_block()
    except Exception as exc:  # noqa: BLE001
        official = {"error": f"Fed.gov/NY Fed 不可达：{exc}"}
    data["official"] = official

    # 权威信号层：ZQ + 即期锚 → 概率
    probs: list[dict] = []
    zq: dict = {}
    spot: dict = {"rate": None, "method": "不可用"}
    try:
        zq = _fetch_zq_quotes()
        meetings = _all_meetings()
        now = datetime.now(ET)
        upcoming = [m for m in meetings if m["end"] > now]
        stmt = official.get("latest_statement")
        if not isinstance(stmt, dict):
            stmt = {}
        spot = _spot_rate(zq, _effr_history(), meetings, stmt)
        probs = _fedwatch_probs(zq, spot, upcoming[:4])
        data["futures"] = {
            "spot": spot, "quotes": zq, "probs": probs,
            "note": ("CME FedWatch 等价口径：ZQ 合约价 × CME 官方方法论自算；"
                     "非 tick 级实时，与 CME 官网数值可能有数个百分点差异"),
        }
    except Exception as exc:  # noqa: BLE001
        data["futures"] = {"error": f"ZQ/EFFR 数据不可达：{exc}",
                           "spot": spot, "quotes": zq, "probs": probs}

    # 预测市场层
    try:
        data["polymarket"] = _pm_block()
    except Exception as exc:  # noqa: BLE001
        data["polymarket"] = {"error": f"Polymarket 不可达：{exc}",
                              "decisions": [], "counts": [], "level": []}

    # CME 官方口径存档（交叉校验层；第三方存档可能滞后，失败不阻塞）
    try:
        data["cme_archive"] = _cme_archive_block()
    except Exception as exc:  # noqa: BLE001
        data["cme_archive"] = {"error": str(exc)}

    # last-good 字段级合并：ZQ 限流时段（quotes 空）不覆盖快照里已有的旧 quotes/probs，
    # 仅更新时间戳与 PM 侧数据。避免一次限流窗口把好数据洗掉。
    fut = data.get("futures") or {}
    if not fut.get("quotes"):
        prev = _load_snapshot()
        prev_data = (prev[1] or {}) if prev else {}
        prev_fut = prev_data.get("futures") or {}
        if prev_fut.get("quotes"):
            fut["quotes"] = prev_fut["quotes"]
            fut["spot"] = prev_fut.get("spot") or fut.get("spot")
            fut["probs"] = prev_fut.get("probs") or []
            fut["stale_quotes"] = True  # 前端标注「ZQ 数据为限流前的快照」
            probs = fut["probs"]
    if not (data.get("polymarket") or {}).get("decisions"):
        prev = _load_snapshot()
        prev_data = (prev[1] or {}) if prev else {}
        prev_pm = prev_data.get("polymarket") or {}
        if prev_pm.get("decisions"):
            data["polymarket"] = prev_pm

    # 边际变化 + 对比矩阵
    try:
        marginal = _marginal_changes(probs, data["polymarket"].get("decisions") or [])
    except Exception:  # noqa: BLE001
        marginal = {"zq": {}, "pm": {}, "history_note": "历史数据不可用"}
    data["marginal"] = marginal
    try:
        data["matrix"] = _build_matrix(
            probs, data["polymarket"].get("decisions") or [], marginal)
    except Exception as exc:  # noqa: BLE001
        data["matrix"] = []

    # 数据源健康（source-health 页口径）
    data["source_status"] = [
        {"key": "fedwatch:fomc_cal", "label": "Fed.gov FOMC 日历",
         "status": "fresh" if official.get("meetings") else "missing"},
        {"key": "fedwatch:effr", "label": "NY Fed EFFR",
         "status": "fresh" if official.get("effr") else "missing"},
        {"key": "fedwatch:zq", "label": "ZQ 联邦基金期货（Yahoo）",
         "status": "fresh" if zq else "missing"},
        {"key": "fedwatch:polymarket", "label": "Polymarket 预测市场",
         "status": "fresh" if data["polymarket"].get("decisions") else "missing"},
        {"key": "fedwatch:cme_archive", "label": "CME 官方口径存档（交叉校验）",
         "status": "fresh" if (data.get("cme_archive") or {}).get("meetings") else "missing"},
    ]

    # 快照历史追加（成功拿到概率才记）
    if probs:
        try:
            _hist_append(probs, data["polymarket"].get("decisions") or [])
        except Exception:  # noqa: BLE001
            pass
    return data


# ---------------------------------------------------------------------------
# 缓存、调度与暴露
# ---------------------------------------------------------------------------

def _load_snapshot():
    if not os.path.exists(_SNAPSHOT):
        return None
    try:
        snap = json.load(open(_SNAPSHOT))
        val = snap.get("data")
        return (snap.get("updated_ts") or 0, val) if val is not None else None
    except Exception:  # noqa: BLE001
        return None


def _save_snapshot(data: dict) -> None:
    try:
        os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
        json.dump({"updated_ts": time.time(), "data": data},
                  open(_SNAPSHOT, "w"), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def get_fedwatch(force: bool = False) -> dict:
    """主入口：SWR 缓存（2 分钟 TTL）+ 磁盘快照持久化。"""
    return cache_runtime.get(
        "fedwatch_v1", lambda: _build_snapshot(),
        valid=lambda v: bool((v.get("futures") or {}).get("probs")
                             or (v.get("polymarket") or {}).get("decisions")),
        warm=_load_snapshot,
        ttl=_TTL_FAST,
        save=_save_snapshot,
        force=force,
    )


_SCHED_STARTED = False
_SCHED_LOCK = threading.Lock()


def start_scheduler(interval: int = 600) -> None:
    """后台每 10 分钟刷新一次：持续积累 ZQ 侧 24h/7d 边际变化历史。"""
    global _SCHED_STARTED
    with _SCHED_LOCK:
        if _SCHED_STARTED:
            return
        _SCHED_STARTED = True

    def _run():
        while True:
            try:
                get_fedwatch(force=True)
            except Exception:  # noqa: BLE001 — 调度失败下轮再试
                pass
            time.sleep(interval)

    threading.Thread(target=_run, daemon=True,
                     name="sched:fedwatch").start()


def warmup() -> None:
    """启动预热（后台线程，失败静默）。"""
    def _run():
        try:
            get_fedwatch()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_run, daemon=True, name="warm:fedwatch").start()


if __name__ == "__main__":
    # 独立运行验证：python fedwatch.py
    snap = _build_snapshot()
    print(json.dumps(snap, ensure_ascii=False, indent=2)[:8000])
    print("\n=== 概率速览 ===")
    for row in snap.get("matrix") or []:
        cme = row.get("cme_equiv") or {}
        pm = row.get("polymarket") or {}
        print(f"{row['meeting']}  implied={row.get('implied_rate')}%  "
              f"自算: hike={cme.get('p_hike')} hold={cme.get('p_hold')} cut={cme.get('p_cut')}  "
              f"PM: hike={pm.get('p_hike')} hold={pm.get('p_hold')} cut={pm.get('p_cut')}  "
              f"diff={row.get('diff')}")
