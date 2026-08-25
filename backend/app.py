"""Market Workbench 后端 —— A股数据层 HTTP 接口（FastAPI）。

端点全部在 /api 下，前端 vite 代理 /api → localhost:8900。
只读、无状态、按用户传入代码返回客观数据。不预置标的、不建议。

启动：
    uvicorn app:app --host 127.0.0.1 --port 8900
"""

from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import astock
import bonds as bonds_layer
import chat as chat_layer
import cli_runtime
import gstock
import fund
import fund_pfs
import fund_portfolio as fpf
import newsradar
import portfolio as pf
import timing
import timing_alloc
import users
import market
import gold_score as gold_score_layer
import oil as oil_layer
import myreports as mr
import reflection as reflect_layer
import plate_scores as plate_scores_layer
import sector_scores as sector_scores_layer
import sw_level2_scores as sw_level2_layer
import industry_chain as industry_chain_layer
import tools as tools_layer
import cache_runtime
import stock_cache
import score_scheduler
import pulse.market_pulse as pulse_market_pulse

from version import read_version

__version__ = read_version()

app = FastAPI(title="Market Workbench API", version=__version__)

# 每半小时后台刷新持仓数据（场内证券 + 场外基金；真重算收益并写缓存，
# 用户点进持仓页 GET 直接返回刷新好的结果。窗口内判断在各自模块内）
pf.start_scheduler(300, users.user_ids)
fpf.start_scheduler(120, users.user_ids)
fund_pfs.start_scheduler()
newsradar.start_scheduler()
score_scheduler.start(
    lambda: sector_scores_layer.get_sector_scores(force=True),
    lambda: sw_level2_layer.get_level2_scores(force=True),
    lambda: plate_scores_layer.get_plate_scores(force=True),
)

# CORS：默认放开（本地自托管友好）；公网部署时用 VR_ALLOW_ORIGINS 收紧成白名单。
#   例：VR_ALLOW_ORIGINS="https://myhost"  （逗号分隔多个）
_ORIGINS = [o.strip() for o in os.environ.get("VR_ALLOW_ORIGINS", "*").split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
# GZip：债市框架/分品种等大 payload（含 3 年趋势序列）压缩传输，184KB → ~27KB
app.add_middleware(GZipMiddleware, minimum_size=1024)

# 可选鉴权：设了 VR_API_KEY 就要求所有 /api/* 带 `Authorization: Bearer <key>`
#   （本地自托管不设=开放；公网部署务必设，否则别人能读你的持仓/调你的后端）。
_API_KEY = os.environ.get("VR_API_KEY", "").strip()

# 公开注册开关：默认关（账号由管理员用 backend/add_user.py 后台添加）。
#   首次启动用户库为空时放行一次，方便新部署把主账号建出来；之后只能走后台添加。
#   VR_ALLOW_REGISTRATION=1 可显式重新打开网页注册。
_REGISTRATION = os.environ.get("VR_ALLOW_REGISTRATION", "").strip().lower() in ("1", "true", "yes", "on")


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    if (
        _API_KEY
        and request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path != "/api/health"
    ):
        bearer_ok = request.headers.get("authorization", "") == f"Bearer {_API_KEY}"
        if request.headers.get("x-vr-access-key", "") != _API_KEY and not bearer_ok:
            return JSONResponse({"detail": "未授权：缺少或错误的 API Key（VR_API_KEY）"}, status_code=401)
    return await call_next(request)


def _bearer_token(request: Request) -> str:
    direct = request.headers.get("x-vr-user-token", "").strip()
    if direct:
        return direct
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _current_user(request: Request) -> dict:
    """从 Authorization: Bearer <token> 解析当前登录用户；未登录抛 401。"""
    user = users.resolve_token(_bearer_token(request))
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return user


def _optional_user_id(request: Request) -> int | None:
    token = request.headers.get("x-vr-user-token", "").strip()
    if not token:
        return None
    user = users.resolve_token(token)
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return int(user["id"])


def _portfolio_user_id(request: Request) -> int:
    """Portfolio ledgers are account-owned; the legacy global file is import-only."""
    return int(_current_user(request)["id"])

_CODE_RE = r"^\d{6}$"


def _validate(code: str) -> str:
    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "代码必须是 6 位数字")
    return code


@app.get("/api/health")
def health():
    return {"ok": True, "service": "market-workbench-api", "version": __version__}


@app.get("/api/source-health")
def source_health(force: bool = Query(False), source: str | None = Query(None)):
    """数据源健康检查：11 个上游探活（60s 节流）+ 各页面缓存/快照时间戳。

    source=<key> 只重探单个上游（单源按钮），其余沿用上次结果。
    """
    import source_health

    only = [s.strip() for s in source.split(",") if s.strip()] if source else None
    try:
        report = source_health.build_report(force=force, only=only)
    except Exception as e:  # noqa: BLE001 — 探活层自身兜底，不该把页面打挂
        raise HTTPException(502, f"健康检查异常：{e}") from e
    return {"data": {**report, "version": __version__}}


# ---------------- 用户体系：注册 / 登录 / 会话 / 每用户数据 ----------------

class AuthReq(BaseModel):
    username: str
    password: str


class DataSetReq(BaseModel):
    key: str
    value: object


class DataMergeReq(BaseModel):
    items: dict


@app.get("/api/auth/config")
def auth_config():
    """登录页公开配置：注册是否开放（空用户库时恒真，保证新部署能建出主账号）。"""
    open_reg = _REGISTRATION or users.user_count() == 0
    return {"data": {"registration_open": open_reg}}


@app.post("/api/auth/register")
def auth_register(req: AuthReq):
    """注册（默认关闭，账号由 add_user.py 后台添加；空库首个账号除外）。"""
    if not (_REGISTRATION or users.user_count() == 0):
        raise HTTPException(403, "注册已关闭，请联系管理员添加账号")
    try:
        user = users.register(req.username, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 注册即登录，直接发 token
    try:
        sess = users.login(req.username, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"data": {**sess, "user_id": user["id"], "first_user": users.user_count() == 1}}


@app.post("/api/auth/login")
def auth_login(req: AuthReq):
    try:
        sess = users.login(req.username, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"data": sess}


@app.get("/api/auth/me")
def auth_me(request: Request):
    return {"data": _current_user(request)}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    users.logout(_bearer_token(request))
    return {"data": {"ok": True}}


@app.get("/api/auth/data")
def auth_get_data(request: Request):
    user = _current_user(request)
    return {"data": users.get_data(user["id"])}


@app.post("/api/auth/data/set")
def auth_set_data(req: DataSetReq, request: Request):
    user = _current_user(request)
    try:
        result = users.set_data(user["id"], req.key, req.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"data": result}


@app.post("/api/auth/data/merge")
def auth_merge_data(req: DataMergeReq, request: Request):
    """首次登录后把浏览器本地数据整体迁移到账号下。"""
    user = _current_user(request)
    return {"data": users.merge_data(user["id"], req.items)}


# ---------------- 用户数据导入 / 导出（备份迁移：换电脑、换部署、账号间搬家） ----------------

class ImportReq(BaseModel):
    payload: dict
    mode: str = "merge"  # merge=只补缺失 key；replace=以备份为准整体替换
    ledgers_mode: str = "skip"  # skip=不动账本；merge=并入当前账本；replace=覆盖账本


@app.get("/api/auth/export")
def auth_export(request: Request):
    """导出当前账号的用户数据 + 两个持仓账本（不含密码哈希/会话/AI key）。

    LLM 配置里的 apiKey 导出前抹掉——备份文件会离开本机，key 不该跟着走。"""
    user = _current_user(request)
    uid = int(user["id"])
    export = users.export_data(uid, user["username"])
    llm = export["data"].get("llm")
    if isinstance(llm, dict):
        export["data"]["llm"] = {**llm, "apiKey": ""}
    export["user"] = {"username": user["username"]}  # 不带 user id（导入只认内容不认 id）
    export["ledgers"] = {
        "portfolio": pf._load(uid),
        "fund_portfolio": fpf._load(uid),
    }
    return {"data": export}


def _merge_ledgers(current: dict, incoming: dict) -> dict:
    """账本合并：当前持仓优先，导入文件只补当前没有的代码；清仓记录按代码去重。"""
    merged = dict(incoming)  # 以导入文件为底，保留其元数据字段
    seen = {h.get("code") for h in current.get("holdings", [])}
    merged["holdings"] = list(current.get("holdings", [])) + [
        h for h in incoming.get("holdings", []) if h.get("code") not in seen
    ]
    closed_codes = {c.get("code") for c in current.get("closed", [])}
    merged["closed"] = list(current.get("closed", [])) + [
        c for c in incoming.get("closed", []) if c.get("code") not in closed_codes
    ]
    return merged


def _sanitize_ledger(d: dict) -> dict:
    """导入前剥掉备份里的派生元数据（年初基准/刷新时间戳/版本号）：
    这些是导出当时算出来的，落在别的账号上未必对；清掉让下次刷新按本账号重建。
    _save 会补上 version/ledger_updated_at。"""
    cleaned = {k: v for k, v in d.items()
               if k not in ("ytd_open", "ytd_year", "ytd_refresh_date",
                            "last_refresh", "version", "ledger_updated_at")}
    return cleaned


@app.post("/api/auth/import")
def auth_import(req: ImportReq, request: Request):
    """导入 export 文件。user_data 按 merge/replace；账本按 skip/merge/replace。"""
    user = _current_user(request)
    uid = int(user["id"])
    try:
        result = users.import_data(uid, req.payload, merge=req.mode != "replace")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    ledgers = req.payload.get("ledgers") or {}
    if req.ledgers_mode != "skip" and isinstance(ledgers, dict):
        pf_in = ledgers.get("portfolio")
        fpf_in = ledgers.get("fund_portfolio")
        try:
            if req.ledgers_mode == "merge":
                if isinstance(pf_in, dict):
                    pf.replace_ledger(_sanitize_ledger(_merge_ledgers(pf._load(uid), pf_in)), uid)
                if isinstance(fpf_in, dict):
                    fpf.replace_ledger(_sanitize_ledger(_merge_ledgers(fpf._load(uid), fpf_in)), uid)
            elif req.ledgers_mode == "replace":
                if isinstance(pf_in, dict):
                    pf.replace_ledger(_sanitize_ledger(pf_in), uid)
                if isinstance(fpf_in, dict):
                    fpf.replace_ledger(_sanitize_ledger(fpf_in), uid)
            else:
                raise ValueError(f"未知账本导入模式: {req.ledgers_mode}")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    result["ledgers_mode"] = req.ledgers_mode
    return {"data": result}


class LLMConfig(BaseModel):
    provider: str = ""       # cli-* = 订阅接入（调本机 CLI）；其余 = API 接入
    baseURL: str = ""        # 订阅接入时留空
    apiKey: str = ""         # 订阅接入时留空
    model: str


class ChatReq(BaseModel):
    messages: list[dict]
    context: str = ""
    llm: LLMConfig


@app.post("/api/chat")
def chat(req: ChatReq):
    """系统 AI 对话，**流式** NDJSON（每行一个事件 {type: tool|delta|done|error}）。

    - API 接入：OpenAI 兼容 function-calling，边流答案边推工具调用事件。
    - 订阅接入（provider=cli-*）：调本机已登录的 CLI，stdout 边出边流（数据靠 context）。
    配置错误（缺 key / 未装 CLI）走 HTTP 400；运行时错误走流内 error 事件。用户配置随请求传入，后端不持久化。
    """
    if not req.messages:
        raise HTTPException(400, "messages 不能为空")
    if not req.llm.model:
        raise HTTPException(400, "缺少模型配置，请先在「接入 AI」里选择")

    is_cli = req.llm.provider.startswith("cli-")
    if is_cli:
        kind = req.llm.provider[4:]
        if not cli_runtime.detect_cli(kind):
            raise HTTPException(400, f"未检测到「{kind}」对应的本机命令。请先安装并登录该 CLI，或改用「API 接入」。")
    elif not req.llm.apiKey or not req.llm.baseURL:
        raise HTTPException(400, "缺少 Base URL 或 API Key，请先在「接入 AI」里填写")

    cfg = req.llm.model_dump()

    def gen():
        try:
            events = (chat_layer.run_chat_cli_stream if is_cli else chat_layer.run_chat_stream)(cfg, req.messages, req.context)
            for ev in events:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001 — 运行时错误以流内事件上报，不中断连接
            yield json.dumps({"type": "error", "message": f"对话失败：{e}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


def _check_llm(llm: LLMConfig) -> dict:
    """校验模型配置并返回 cfg（chat / reflect 两个流式端点共用）。

    配置问题走 HTTP 400（前端能弹提示引导去「接入 AI」页），运行时错误留给流内 error 事件。
    """
    if not llm.model:
        raise HTTPException(400, "缺少模型配置，请先在「接入 AI」里选择")
    if llm.provider.startswith("cli-"):
        kind = llm.provider[4:]
        if not cli_runtime.detect_cli(kind):
            raise HTTPException(400, f"未检测到「{kind}」对应的本机命令。请先安装并登录该 CLI，或改用「API 接入」。")
    elif not llm.apiKey or not llm.baseURL:
        raise HTTPException(400, "缺少 Base URL 或 API Key，请先在「接入 AI」里填写")
    return llm.model_dump()


def _ndjson(events):
    """把事件生成器包成 NDJSON 流；运行时异常转成流内 error 事件，不中断连接。"""
    def gen():
        try:
            for ev in events():
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


class ReflectReq(BaseModel):
    source: str
    title: str = ""
    llm: LLMConfig


@app.post("/api/reflect")
def reflect(req: ReflectReq):
    """反思：对一段已写好的分析做推理审计（哪些有数据支撑、最脆弱一环、验证清单），流式 NDJSON。"""
    if not (req.source or "").strip():
        raise HTTPException(400, "source 不能为空")
    cfg = _check_llm(req.llm)
    return _ndjson(lambda: reflect_layer.run_reflection_stream(cfg, req.source, req.title))


class HoldingIn(BaseModel):
    code: str
    shares: float
    cost: float
    # 可省：买入日期（YYYY-MM-DD）。年后买的按成本计本年盈亏，年前买的按年初价计
    bought_date: Optional[str] = None


@app.get("/api/portfolio")
def portfolio_get(request: Request, fresh: bool = Query(False)):
    """持仓 + 盈亏（浮动盈亏红涨绿跌）。默认返回缓存快照秒开；fresh=true 真重算。"""
    try:
        return {"data": pf.get_portfolio(fresh=fresh, user_id=_portfolio_user_id(request))}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"持仓读取异常：{e}") from e


@app.post("/api/portfolio/holding")
def portfolio_add(h: HoldingIn, request: Request):
    """加一笔持仓（同代码按加权平均成本合并）。存本地，不上传。"""
    code = (h.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "代码必须是 6 位数字")
    if h.shares <= 0:
        raise HTTPException(400, "数量必须大于 0")
    # 成本价不限正负：融券 / 返息 / 摊薄后为负成本等情形按结果计算，用户想怎么输就怎么输。
    bd = (h.bought_date or "").strip()
    if bd:
        try:
            datetime.strptime(bd, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "买入日期格式应为 YYYY-MM-DD") from None
    return {"data": pf.add_holding(code, h.shares, h.cost, bd or None, _portfolio_user_id(request))}


@app.delete("/api/portfolio/holding")
def portfolio_remove(request: Request, code: str = Query(...)):
    return {"data": pf.remove_holding(code.strip(), _portfolio_user_id(request))}


# ---- 我的研报（用户上传自己的研报，存本地、不上传、不进开源仓库）----

class ReportIn(BaseModel):
    name: str
    content_b64: str


@app.get("/api/myreports")
def myreports_list():
    return {"data": mr.list_reports()}


@app.post("/api/myreports")
def myreports_upload(r: ReportIn):
    """上传一份研报（base64）→ 存本地 + 按文件名自动打行业标签。"""
    try:
        return {"data": mr.save_report(r.name, r.content_b64)}
    except mr.ReportError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/myreports/file/{rid}")
def myreports_file(rid: str):
    """下载/预览某份研报原文件。"""
    hit = mr.report_path(rid)
    if not hit:
        raise HTTPException(404, "研报不存在")
    path, name = hit
    return FileResponse(str(path), filename=name)


@app.delete("/api/myreports/{rid}")
def myreports_delete(rid: str):
    return {"data": {"ok": mr.delete_report(rid)}}


class CloseIn(BaseModel):
    code: str
    date: str
    price: float
    shares: float
    # 可省：缺省时用当前持仓的加权成本（添加持仓时已录过成本，清仓不必重填）
    cost: Optional[float] = None


@app.post("/api/portfolio/close")
def portfolio_close(c: CloseIn, request: Request):
    """记一笔已清仓（已实现盈亏），并从当前持仓扣减对应股数。存本地。"""
    code = (c.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "代码必须是 6 位数字")
    if c.price <= 0 or c.shares <= 0:
        raise HTTPException(400, "清仓价与股数必须大于 0")
    # 买入成本不限正负（同持仓录入）：按 (清仓价 - 成本) × 股数 的结果计算已实现盈亏。
    date = (c.date or "").strip()
    if not date:
        raise HTTPException(400, "请填清仓日期")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "清仓日期格式应为 YYYY-MM-DD") from None
    try:
        return {"data": pf.close_position(code, date, c.price, c.shares, c.cost, _portfolio_user_id(request))}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/portfolio/close")
def portfolio_close_remove(request: Request, index: int = Query(...)):
    return {"data": pf.remove_closed(index, _portfolio_user_id(request))}


@app.post("/api/portfolio/refresh")
def portfolio_refresh(request: Request):
    """手动刷新：真重算持仓收益并覆盖缓存。"""
    try:
        return {"data": pf.get_portfolio(fresh=True, user_id=_portfolio_user_id(request))}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"刷新失败：{e}") from e


@app.get("/api/portfolio/timing")
def portfolio_timing(request: Request):
    """当前持仓的加减仓信号 + 强度（按 research/A股优质个股中短期择时策略.md 规则，
    前复权日 K 计算）。规则化技术指标提示，非投资建议。"""
    try:
        holdings = pf._load(_portfolio_user_id(request)).get("holdings", [])
        codes = [h["code"] for h in holdings]
        return {"data": {"signals": timing.get_timing_signals(codes)}}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"择时信号异常：{e}") from e



# ---------------------------------------------------------------------------
# 基金模块（公募：搜索 / 实时估值 / 净值走势 / 指标 / 筛选 / 持仓账本）
# ---------------------------------------------------------------------------


@app.get("/api/funds/search")
def funds_search(q: str = Query(...), limit: int = Query(20, le=50)):
    """基金模糊搜索（代码/简称/拼音）。"""
    try:
        return {"data": fund.search_funds(q, limit)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基金搜索异常：{e}") from e


@app.get("/api/funds/quote")
def funds_quote(codes: str = Query(...)):
    """批量实时估值 + 最新净值（逗号分隔代码，最多 50 只）。"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:50]
    try:
        return {"data": fund.realtime_estimates(code_list)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基金行情异常：{e}") from e


@app.get("/api/funds/nav/{code}")
def funds_nav(code: str, limit: int = Query(250, le=4000)):
    """单位净值走势（升序）。"""
    try:
        return {"data": fund.nav_history(code, limit)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"净值走势异常：{e}") from e


@app.get("/api/funds/metrics/{code}")
def funds_metrics(code: str):
    """近一年业绩指标：年化/最大回撤/波动率/夏普（净值序列自算）。"""
    try:
        return {"data": fund.fund_metrics(code)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基金指标异常：{e}") from e


@app.get("/api/funds/profile/{code}")
def funds_profile(code: str):
    """基金档案：基本信息 + 最新十大重仓 + 业绩指标。"""
    try:
        return {"data": fund.fund_profile(code)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基金档案异常：{e}") from e


@app.get("/api/funds/screen")
def funds_screen(type: str = Query(""), r4433: bool = Query(False),
                 sort_by: str = Query("近1年"), order: str = Query("desc"),
                 min_y1: float | None = Query(None), min_m6: float | None = Query(None),
                 min_y3: float | None = Query(None), keyword: str = Query(""),
                 limit: int = Query(100, le=500)):
    """全市场基金业绩筛选（支持 4433 法则与业绩下限过滤）。"""
    try:
        return {"data": fund.screen_funds(type, r4433, sort_by, order,
                                          min_y1, min_m6, min_y3, keyword, limit)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基金筛选异常：{e}") from e


@app.get("/api/funds/pfs")
def funds_pfs(strategy: str = Query(""), tier: str = Query(""), pool: str = Query(""),
              keyword: str = Query(""), limit: int = Query(100, le=200),
              refresh: bool = Query(False)):
    """PFS V3.0 Manager-First 主动权益基金公开数据初筛。"""
    try:
        return {"data": fund_pfs.query_pfs(strategy, tier, pool, keyword, limit, refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"PFS 构建异常：{e}") from e


class FundHoldingIn(BaseModel):
    code: str
    shares: float
    cost: float
    # 可省：买入日期（YYYY-MM-DD）。年后买的按成本计本年盈亏，年前买的按年初净值计
    bought_date: Optional[str] = None


@app.get("/api/fund-portfolio")
def fund_portfolio_get(request: Request, fresh: bool = Query(False)):
    """基金持仓 + 最新净值/盘中估值叠加浮动盈亏。默认返回缓存快照秒开；fresh=true 真重算。"""
    try:
        return {"data": fpf.get_portfolio(bypass=fresh, user_id=_portfolio_user_id(request))}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基金持仓读取异常：{e}") from e


@app.post("/api/fund-portfolio/holding")
def fund_portfolio_add(h: FundHoldingIn, request: Request):
    """加一笔基金持仓（份额 + 单位成本净值）；同代码按加权平均合并。"""
    code = (h.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "基金代码必须是 6 位数字")
    if h.shares <= 0:
        raise HTTPException(400, "份额必须大于 0")
    bd = (h.bought_date or "").strip()
    if bd:
        try:
            datetime.strptime(bd, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "买入日期格式应为 YYYY-MM-DD") from None
    return {"data": fpf.add_holding(code, h.shares, h.cost, bd or None, _portfolio_user_id(request))}


@app.delete("/api/fund-portfolio/holding")
def fund_portfolio_remove(request: Request, code: str = Query(...)):
    return {"data": fpf.remove_holding(code.strip(), _portfolio_user_id(request))}


class FundCloseIn(BaseModel):
    code: str
    date: str
    nav: float
    shares: float
    cost: float | None = None


@app.post("/api/fund-portfolio/close")
def fund_portfolio_close(c: FundCloseIn, request: Request):
    """记一笔已卖出（按卖出净值算已实现盈亏），并扣减当前份额。"""
    code = (c.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "基金代码必须是 6 位数字")
    if c.nav <= 0 or c.shares <= 0:
        raise HTTPException(400, "卖出净值与份额必须大于 0")
    date = (c.date or "").strip()
    if not date:
        raise HTTPException(400, "请填卖出日期")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "卖出日期格式应为 YYYY-MM-DD") from None
    try:
        return {"data": fpf.close_position(code, date, c.nav, c.shares, c.cost, _portfolio_user_id(request))}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/fund-portfolio/close")
def fund_portfolio_close_remove(request: Request, index: int = Query(...)):
    return {"data": fpf.remove_closed(index, _portfolio_user_id(request))}


@app.get("/api/portfolio/legacy-status")
def portfolio_legacy_status(request: Request):
    uid = int(_current_user(request)["id"])
    return {"data": {"securities": pf.legacy_status(uid), "fund": fpf.legacy_status(uid)}}


@app.post("/api/portfolio/import-legacy")
def portfolio_import_legacy(request: Request, kind: str = Query(pattern="^(securities|fund)$")):
    uid = int(_current_user(request)["id"])
    try:
        return {"data": pf.import_legacy(uid) if kind == "securities" else fpf.import_legacy(uid)}
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/radar")
def radar():
    """资讯雷达：12 赛道公开 RSS 资讯（读缓存，无缓存返回赛道骨架）。"""
    try:
        return {"data": newsradar.get_radar(force=False)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资讯雷达异常：{e}") from e


@app.post("/api/radar/refresh")
def radar_refresh():
    """强制重抓全部 RSS 源（耗时约 20-40s），更新缓存。"""
    try:
        return {"data": newsradar.get_radar(force=True)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资讯雷达刷新失败：{e}") from e


@app.get("/api/public-news-search")
def public_news_search(
    q: str = Query(..., min_length=2, max_length=160),
    count: int = Query(5, ge=1, le=8),
):
    """重大事件公开资料联网核验：固定 Bing RSS 搜索，只返回标题、摘要与来源链接。"""
    try:
        return {"data": tools_layer.public_news_search(q, count)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"公开资料搜索失败：{e}") from e


@app.get("/api/market/overview")
def market_overview():
    """市场情绪 + 板块资金流（板块/大盘级，全站共享缓存 5 分钟）。"""
    try:
        return {"data": market.get_overview()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"市场总览异常：{e}") from e


@app.get("/api/sector-scores")
def sector_scores(refresh: bool = Query(False)):
    """行业评分：申万一级行业的估值、盈利景气、资本活跃和集中风险。

    首次构建读取申万 2021 版分类启用后的月报，并以申万成分分类叠加
    a-stock-data 腾讯个股行情聚合当前值；盘中缓存 5 分钟、其他时段 1 小时。
    申万日频只作备用，refresh=true 会强制刷新当前评分。
    """
    try:
        return {"data": sector_scores_layer.get_sector_scores(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"板块评分异常：{e}") from e


@app.get("/api/sector-scores/cache")
def sector_scores_cache():
    """行业评分最近一次成功缓存；供前端在后台刷新期间即时展示。"""
    return {"data": sector_scores_layer.get_cached_sector_scores()}


@app.get("/api/sector-scores/level2")
def sector_scores_level2(refresh: bool = Query(False)):
    """申万二级行业（2021 版 131 个）指标与一级映射。

    当前值取申万二级行业最近交易日日频，历史分位锚取申万月报（约 60 个月）。
    盘中缓存 5 分钟、其他时段 1 小时；refresh=true 强制刷新。
    """
    try:
        return {"data": sw_level2_layer.get_level2_scores(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"二级行业指标异常：{e}") from e


@app.get("/api/sector-scores/level2/cache")
def sector_scores_level2_cache():
    """二级行业指标最近一次成功缓存；供前端在后台刷新期间即时展示。"""
    return {"data": sw_level2_layer.get_cached_level2_scores()}


@app.get("/api/plate-scores")
def plate_scores(refresh: bool = Query(False)):
    """板块双评分：30 个主题板块的强度分 + 机会分（防追涨体系）。

    成分股来自人工维护的板块主数据（sectorResearch 代表企业 + 公开龙头），
    行情走腾讯批量接口，基准为中证全指。盘中缓存 5 分钟、其他时段 1 小时。
    refresh=true 强制刷新。
    """
    try:
        return {"data": plate_scores_layer.get_plate_scores(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"板块评分异常：{e}") from e


@app.get("/api/plate-scores/cache")
def plate_scores_cache():
    """板块评分最近一次成功缓存；供前端在后台刷新期间即时展示。"""
    return {"data": plate_scores_layer.get_cached_plate_scores()}


@app.get("/api/industry-chains")
def industry_chains():
    """产业链目录（静态主数据）：哪些板块已梳理产业链、环节与公司数量。"""
    try:
        return {"data": industry_chain_layer.list_chains()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"产业链目录异常：{e}") from e


@app.get("/api/industry-chain/{key}")
def industry_chain(key: str, refresh: bool = Query(False)):
    """产业链纵深聚合：图谱结构 / 各环节代表公司利润分布 / 瓶颈 / 景气传导 / 行业研报。

    链结构为人工维护快照（backend/data/industry_chains.json）；利润分布按
    同花顺财务摘要批量抓取（每日缓存，TTL 12 小时）。各块独立降级。refresh=true 强制刷新。
    """
    try:
        return {"data": industry_chain_layer.get_chain(key, force=refresh)}
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"产业链聚合异常：{e}") from e


@app.get("/api/market/emotion")
def market_emotion():
    """短线情绪：连板梯队 / 最高连板 / 炸板率 / 封板率 / 晋级率 / 涨跌停家数。

    含连板梯队个股清单（code/name/连板数等）——2026-07-05 起如实展示客观公开榜单（东财同款），
    只呈现事实，不附推荐/评分/预测/买卖时机。全站共享缓存 5 分钟。
    """
    try:
        return {"data": market.get_short_term_emotion()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"短线情绪异常：{e}") from e


@app.get("/api/market/turnover-top")
def market_turnover_top():
    """全市场成交额榜 Top20（客观公开榜单数据，非推荐/非预测/不评分）。全站共享缓存 5 分钟。"""
    try:
        return {"data": market.get_turnover_top()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"成交额榜异常：{e}") from e


@app.get("/api/market/liquidity")
def market_liquidity(refresh: bool = False):
    """资金供给重要指标（国内：两融 / 主力净流入；国外：美债 10Y / 5Y / 3M 与 10Y-3M 利差）。缓存 5 分钟。"""
    try:
        return {"data": market.get_liquidity(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资金供给指标异常：{e}") from e


# 全球指数分时 60 秒缓存（数据源分钟级）
_GLOBAL_MINUTE_CACHE: dict = {}
_A_MINUTE_CACHE: dict = {}


@app.get("/api/global/indices")
def global_indices(keys: str | None = Query(default=None, max_length=500)):
    """全球指数快照（美股 / 港股 / 亚太 / 欧洲 / 南亚）—— A 股看隔夜外围脸色。缓存 5 分钟。

    keys（逗号分隔）非空时只返回这些市场，供「市场全景」每 5 分钟只增量刷新已开盘市场；
    不传时返回全部。
    """
    try:
        if keys:
            return {"data": market.get_global_indices_for([k.strip() for k in keys.split(",") if k.strip()])}
        return {"data": market.get_global_indices()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"全球指数异常：{e}") from e


@app.get("/api/global/minute")
def global_minute(key: str = Query(..., min_length=2, max_length=16)):
    """全球指数当日分时（分钟级）。港股/亚太/南亚腾讯全量分钟优先，
    欧美/其余用东财 push2his 分钟序列（收盘价口径、无成交量）。缓存 5 分钟。"""
    key = key.strip()
    if key not in astock.GLOBAL_INDEX_MINUTE_SRC:
        raise HTTPException(404, f"未覆盖的全球指数「{key}」")
    hit = _GLOBAL_MINUTE_CACHE.get(key)
    if hit and _time.time() - hit[0] < 60:
        return {"data": hit[1]}
    sym, secid = astock.GLOBAL_INDEX_MINUTE_SRC[key]
    try:
        data = astock.minute_kline(sym) if sym else (astock.em_index_minutes(secid) or {"date": "", "prev_close": 0.0, "points": []})
        # 港股腾讯分时缺 16:00 收盘 tick：用东财补一个收盘点，避免图表末端空缺
        if sym and sym.lower().startswith("hk") and data["points"]:
            last_t = data["points"][-1]["time"]
            if last_t < "1600":
                em = astock.em_index_minutes(secid)
                if em and em["points"]:
                    em_last = em["points"][-1]
                    if em_last["time"] >= "1600":
                        data["points"].append({
                            "time": em_last["time"],
                            "price": em_last["price"],
                            "volume": 0,
                        })
        if not data["points"]:
            # 闭市市场当日分钟为空 → 回退「上一交易日」走势（标注 last_day=True）
            fb = astock.em_index_minutes_latest(secid, market_key=key)
            if fb:
                fb["last_day"] = True
                data = fb
        if not data["points"]:
            raise HTTPException(502, "分时数据源当前无返回")
        # 注入该市场完整交易时段（北京分钟数），供前端分时图 x 轴覆盖完整交易时段
        mm = gstock.market_minutes_bj(key)
        if mm:
            data["market_minutes"] = mm
        _GLOBAL_MINUTE_CACHE[key] = (_time.time(), data)
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"全球指数分时异常：{e}") from e


@app.get("/api/global/stock")
def global_stock(symbol: str = Query(..., min_length=1, max_length=16), refresh: bool = False):
    """美股 / 港股个股聚合：行情 + 关键财务指标（东财域内源）。symbol 如 AAPL / BABA / 00700。"""
    try:
        clean = symbol.strip().upper()
        data = _stock_cached("global_stock", clean, 15, lambda: gstock.us_hk_stock(clean), refresh)
        if not data:
            raise HTTPException(404, f"未找到美股/港股代码「{symbol}」")
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"美港股查询异常：{e}") from e


@app.get("/api/global/hk/cashflow")
def global_hk_cashflow(symbol: str = Query(..., min_length=1, max_length=16)):
    """港股现金流量表（东财域内源 RPT_HKSK_FN_CASHFLOW）：经营/投资/筹资/净增加，多期。symbol 如 00700。"""
    try:
        data = gstock.hk_cashflow(symbol.strip())
        if not data:
            raise HTTPException(404, f"未找到港股「{symbol}」的现金流数据（仅港股支持）")
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"港股现金流查询异常：{e}") from e


@app.get("/api/indices")
def indices():
    """A股大盘指数实时行情（上证/深证成指/创业板指/沪深300）。仅标准库。"""
    try:
        return {"data": astock.index_quote()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"指数行情异常：{e}") from e


@app.get("/api/quote")
def quote(codes: str = Query(..., description="逗号分隔的 6 位代码")):
    """实时行情：现价/涨跌/PE/PB/市值/换手/涨跌停。仅标准库，永远可用。"""
    lst = [c.strip() for c in codes.split(",") if c.strip()]
    if not lst or any(not c.isdigit() or len(c) != 6 for c in lst):
        raise HTTPException(400, "codes 必须是逗号分隔的 6 位数字")
    try:
        return {"data": astock.tencent_quote(lst)}
    except Exception as e:  # noqa: BLE001 — 边界统一兜底
        raise HTTPException(502, f"行情源异常：{e}") from e


def _stock_cached(endpoint: str, code: str, ttl: int, fetch, force: bool = False, valid=None):
    value = cache_runtime.get(
        f"stock:{endpoint}:{code}", fetch,
        valid=valid or (lambda value: value is not None), ttl=ttl,
        warm=lambda: stock_cache.warm(endpoint, code),
        save=lambda value: stock_cache.save(endpoint, code, value),
        force=force, decorate=False,
    )
    if value is None or (isinstance(value, dict) and value.get("cache_state") == "error" and value.get("cached_at") is None):
        detail = value.get("refresh_error") if isinstance(value, dict) else None
        raise RuntimeError(detail or "数据源当前无可用结果")
    return value


@app.get("/api/valuation/percentile")
def valuation_percentile(code: str = Query(...), refresh: bool = False):
    """PE-TTM / PB 历史分位（近5年）。全站缓存 30 分钟/代码（历史序列日频、变化慢）。"""
    code = _validate(code)
    try:
        return {"data": _stock_cached("valuation_percentile", code, 24 * 3600, lambda: astock.valuation_percentile(code), refresh, bool)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"估值分位异常：{e}") from e


@app.get("/api/announcements")
def announcements(code: str = Query(...), refresh: bool = False):
    """个股近期公告（东财，仅 requests）。缓存 15 分钟/代码。"""
    code = _validate(code)
    try:
        return {"data": _stock_cached("announcements", code, 900, lambda: astock.announcements(code), refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"公告源异常：{e}") from e


@app.get("/api/financials")
def financials(code: str = Query(...), refresh: bool = False):
    """财务关键指标（同花顺财务摘要，最新报告期）。缓存 30 分钟/代码。"""
    code = _validate(code)
    try:
        return {"data": _stock_cached("financials", code, 24 * 3600, lambda: astock.financials(code), refresh, bool)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"财务摘要异常：{e}") from e


@app.get("/api/valuation")
def valuation(code: str = Query(...), refresh: bool = False):
    """完整估值：行情 + 一致预期 + 前向PE/PEG/消化年数。"""
    code = _validate(code)
    try:
        return {"data": _stock_cached("valuation", code, 6 * 3600, lambda: astock.full_valuation(code), refresh, bool)}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"估值计算异常：{e}") from e


@app.get("/api/reports")
def reports(code: str = Query(...), pages: int = Query(2, ge=1, le=5), refresh: bool = False):
    """个股研报列表（东财，含 PDF 链接）。仅需 requests。"""
    code = _validate(code)
    try:
        def build():
            rows = astock.eastmoney_reports(code, max_pages=pages)
            for row in rows:
                row["pdfUrl"] = astock.pdf_url(row.get("infoCode", "")) if row.get("infoCode") else None
            return rows
        return {"data": _stock_cached(f"reports:{pages}", code, 6 * 3600, build, refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"研报源异常：{e}") from e


@app.get("/api/news")
def news(code: str = Query(...), limit: int = Query(20, ge=1, le=50), refresh: bool = False):
    """个股新闻（东财，需 akshare）。"""
    code = _validate(code)
    try:
        return {"data": _stock_cached(f"news:{limit}", code, 600, lambda: astock.stock_news(code, limit=limit), refresh)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"新闻源异常：{e}") from e


@app.get("/api/info")
def info(code: str = Query(...), refresh: bool = False):
    """个股基本面：行业/股本/上市时间（需 akshare）。"""
    code = _validate(code)
    try:
        return {"data": _stock_cached("info", code, 24 * 3600, lambda: astock.individual_info(code), refresh)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"基本面源异常：{e}") from e


@app.get("/api/disclosure")
def disclosure(code: str = Query(...), refresh: bool = False):
    """巨潮公告列表（需 akshare）。"""
    code = _validate(code)
    try:
        return {"data": _stock_cached("disclosure", code, 900, lambda: astock.disclosure(code), refresh)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"公告源异常：{e}") from e


@app.get("/api/kline")
def kline(code: str = Query(...), category: int = Query(4), offset: int = Query(60, ge=1, le=800)):
    """K线（需 mootdx）。category 4=日 5=周 6=月 11=60分钟。"""
    code = _validate(code)
    try:
        return {"data": astock.kline(code, category=category, offset=offset)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"K线源异常：{e}") from e


@app.get("/api/kline/chart")
def kline_chart(
    code: str = Query(...),
    period: str = Query("day", pattern="^(day|week|month)$"),
    count: int = Query(250, ge=20, le=800),
    refresh: bool = False,
):
    """图表标准 OHLCV：腾讯前复权主源，mootdx 不复权备用。"""
    code = _validate(code)
    try:
        ttl = 300 if period == "day" else 24 * 3600
        data = _stock_cached(
            f"kline:{period}:{count}", code, ttl,
            lambda: astock.chart_kline(code, period=period, count=count), refresh,
            lambda value: bool(value.get("rows")),
        )
        if not data["rows"]:
            raise HTTPException(502, "K线数据源当前无返回")
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"K线源异常：{e}") from e


@app.get("/api/kline/minute")
def kline_minute(code: str = Query(...)):
    """分时图（当日分钟级）：腾讯 minute/query 接口；闭市无数据时回退上一交易日。"""
    # 支持黄金期货品种：AU0/AU9999/AUTD
    code = (code or "").strip()
    # 指数卡传带前缀代码（sh000300），个股页传 6 位裸代码
    # 黄金期货直接跳过验证
    if not code.startswith("AU") and not (len(code) == 8 and code[:2].isalpha() and code[2:].isdigit()):
        code = _validate(code)
    hit = _A_MINUTE_CACHE.get(code)
    if hit and _time.time() - hit[0] < 30:
        return {"data": hit[1]}
    try:
        data = astock.minute_kline(code)
        if not data["points"]:
            secid = astock.a_index_em_secid(code) or astock.a_index_em_secid(f"{astock.get_prefix(code)}{code}")
            fb = astock.em_index_minutes_latest(secid) if secid else None
            if fb:
                fb["last_day"] = True
                data = fb

        # 黄金期货无数据或为 AU 系列：尝试新浪期货接口
        if not data["points"] and code.startswith("AU"):
            data = astock.futures_minute_kline(code)

        if not data["points"]:
            raise HTTPException(502, "分时数据源当前无返回")

        if not data["points"]:
            raise HTTPException(502, "分时数据源当前无返回")
        # A 股指数/个股分时：注入固定交易时段（北京时间 09:30-11:30 / 13:00-15:00），
        # 让前端分时图 x 轴覆盖完整 240 分钟而非仅数据跨度——午休段不前向填充、
        # 收盘时间标注正确（不再把 13:01 数据点误标到 11:31 槽位）。
        if not data.get("market_minutes"):
            data["market_minutes"] = [[570, 690], [780, 900]]
        _A_MINUTE_CACHE[code] = (_time.time(), data)
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"分时源异常：{e}") from e


@app.get("/api/finance")
def finance(code: str = Query(...), refresh: bool = False):
    """季报财务快照（需 mootdx）。"""
    code = _validate(code)
    try:
        return {"data": _stock_cached("finance", code, 24 * 3600, lambda: astock.finance(code), refresh, bool)}
    except astock.DependencyMissing as e:
        raise HTTPException(501, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"财务源异常：{e}") from e


# ---------------------------------------------------------------------------
# 资金面 / 筹码 / 信号（东财数据中心，v3.3 并入）—— 均为「用户查的那只股」的公开数据。
# 东财有 1s 限流，这些多为日/季级静态数据，统一走 30 分钟缓存，进一步降低被封风险。
# ---------------------------------------------------------------------------

def _cached(endpoint: str, code: str, ttl: int, fetch):
    return _stock_cached(endpoint, code, ttl, fetch)


@app.get("/api/margin")
def margin(code: str = Query(...)):
    """融资融券明细（东财，日级）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("margin", code, 1800, lambda: astock.margin_trading(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"融资融券异常：{e}") from e


@app.get("/api/block-trade")
def block_trade(code: str = Query(...)):
    """大宗交易（东财）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("block", code, 1800, lambda: astock.block_trade(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"大宗交易异常：{e}") from e


@app.get("/api/holders")
def holders(code: str = Query(...)):
    """股东户数变化（东财，季度级）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("holders", code, 1800, lambda: astock.holder_num_change(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"股东户数异常：{e}") from e


@app.get("/api/dividend")
def dividend(code: str = Query(...)):
    """分红送转历史（东财）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("dividend", code, 1800, lambda: astock.dividend_history(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"分红送转异常：{e}") from e


@app.get("/api/fund-flow")
def fund_flow(code: str = Query(...)):
    """个股资金流（东财 push2his，120 日主力净流入）。缓存 15 分钟。
    注：push2his 对部分大陆住宅 IP 有间歇风控，可能返回空（非代码问题）。"""
    code = _validate(code)
    try:
        return {"data": _cached("fundflow", code, 900, lambda: astock.stock_fund_flow_120d(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资金流异常：{e}") from e


@app.get("/api/dragon-tiger")
def dragon_tiger(code: str = Query(...)):
    """龙虎榜：该股近期上榜记录 + 买卖席位 + 机构净买（东财）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("dt", code, 1800, lambda: astock.dragon_tiger_board(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"龙虎榜异常：{e}") from e


@app.get("/api/lockup")
def lockup(code: str = Query(...)):
    """限售解禁日历：历史解禁 + 未来 90 天待解禁（东财）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("lockup", code, 1800, lambda: astock.lockup_expiry(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"解禁日历异常：{e}") from e


@app.get("/api/blocks")
def blocks(code: str = Query(...)):
    """个股所属板块/概念归属（东财 slist）。缓存 30 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("blocks", code, 1800, lambda: astock.concept_blocks(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"板块归属异常：{e}") from e


# ---------------------------------------------------------------------------
# 搜索（东财 suggest，支持代码 / 中文 / 拼音首字母）
# ---------------------------------------------------------------------------

_SEARCH_MKT = {
    0: ("", "SZ"),      # 深市
    1: ("", "SH"),      # 沪市
    105: (".O", "NASDAQ"), 106: (".N", "NYSE"), 107: (".O", "US"),
    116: (".HK", "HK"),
    177: (".KS", "KR"),
}


@app.get("/api/search")
def search(q: str = Query(..., min_length=1, max_length=30)):
    """个股搜索：代码 / 中文名 / 拼音首字母。返回匹配列表（最多 8 条）。"""
    q = q.strip()
    if not q:
        return {"data": []}
    try:
        r = astock.em_get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={"input": q, "type": 14,
                    "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 20},
            headers={"User-Agent": astock.UA}, timeout=8,
        )
        rows = (r.json().get("QuotationCodeTable") or {}).get("Data") or []
    except Exception:
        return {"data": []}

    out = []
    seen = set()
    for s in rows:
        try:
            mkt = int(s.get("MktNum"))
        except (TypeError, ValueError):
            continue
        if mkt not in _SEARCH_MKT:
            continue
        suffix, market = _SEARCH_MKT[mkt]
        code = s.get("Code", "")
        name = s.get("Name", "")
        if not code or code in seen:
            continue
        seen.add(code)
        # A股给 6 位纯代码；美/港/韩给带后缀代码
        full_code = f"{code}{suffix}" if suffix else code
        out.append({"code": full_code, "name": name, "market": market})
        if len(out) >= 8:
            break
    return {"data": out}


@app.get("/api/hot-concepts")
def hot_concepts(code: str = Query(...)):
    """个股当下被市场归到哪些概念在炒（东财热门概念命中）。缓存 15 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("hotcon", code, 900, lambda: astock.hot_concepts(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"热门概念异常：{e}") from e


@app.get("/api/investor-qa")
def investor_qa(code: str = Query(...)):
    """互动易问答（巨潮）：投资者提问 + 公司回复。缓存 15 分钟。"""
    code = _validate(code)
    try:
        return {"data": _cached("irm", code, 900, lambda: astock.investor_qa(code))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"互动易异常：{e}") from e


@app.get("/api/industry")
def industry(top: int = Query(20, ge=5, le=50)):
    """全行业涨跌幅排名（东财行业板块，板块级、零个股名单）。缓存 5 分钟。"""
    key = ("industry", str(top))
    hit = _GLOBAL_MINUTE_CACHE.get(key)
    if hit and _time.time() - hit[0] < 300:
        return {"data": hit[1]}
    try:
        data = astock.industry_comparison(top_n=top)
        _GLOBAL_MINUTE_CACHE[key] = (_time.time(), data)
        return {"data": data}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"行业排名异常：{e}") from e


# ---------------------------------------------------------------------------
# 深度分析代理：自选页「AI分析」列，转发到独立部署的 hermes-agent（约 20 分钟级慢任务）。
# 密钥只放后端环境变量，不进前端；前端用 backgroundTasks 轮询等待。
# ---------------------------------------------------------------------------
_DEEP_ANALYSIS_URL = os.environ.get("VR_DEEP_ANALYSIS_URL", "http://192.168.0.231:8642/v1/chat/completions")
_DEEP_ANALYSIS_KEY = os.environ.get("VR_DEEP_ANALYSIS_KEY", "")
_DEEP_ANALYSIS_MODEL = os.environ.get("VR_DEEP_ANALYSIS_MODEL", "hermes-agent")
_DEEP_ANALYSIS_TIMEOUT = int(os.environ.get("VR_DEEP_ANALYSIS_TIMEOUT", "2400"))  # 秒，覆盖 20 分钟级任务


class DeepAnalysisReq(BaseModel):
    prompt: str


@app.post("/api/deep-analysis")
def deep_analysis(req: DeepAnalysisReq):
    """深度分析代理：把 prompt 转发给 hermes-agent，返回纯文本结论。

    hermes-agent 是 20 分钟级慢任务，这里用长 timeout 同步等待；
    前端把每次调用注册进 backgroundTasks，页面跳转/刷新后可继续查看进度。
    """
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    if not _DEEP_ANALYSIS_KEY:
        raise HTTPException(501, "后端未配置 VR_DEEP_ANALYSIS_KEY，无法调用深度分析服务")

    import requests as _requests

    try:
        r = _requests.post(
            _DEEP_ANALYSIS_URL,
            headers={
                "Authorization": f"Bearer {_DEEP_ANALYSIS_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": _DEEP_ANALYSIS_MODEL, "messages": [{"role": "user", "content": prompt}]},
            timeout=_DEEP_ANALYSIS_TIMEOUT,
        )
    except _requests.RequestException as e:
        raise HTTPException(502, f"深度分析服务连接失败：{e}") from e

    if r.status_code != 200:
        raise HTTPException(502, f"深度分析服务 HTTP {r.status_code}: {r.text[:300]}")

    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError) as e:
        raise HTTPException(502, f"深度分析服务返回格式异常：{e}") from e
    return {"data": {"content": content}}


@app.get("/api/pulse/overview")
async def pulse_overview(refresh: bool = Query(False)):
    """全球宏观预期概率总览（Polymarket + Kalshi 双源合并，按模块分组）。

    数据来自两站公开只读 API（免登录、零鉴权），作为全球宏观情绪温度计。
    普通加载直接返回磁盘快照（秒开）；refresh=true 触发后台异步重建（Kalshi
    全量事件书约 1-8 分钟），立即返回当前快照并带 updating=true，前端轮询
    as_of 变化。移植自 https://github.com/simonlin1212/globalpercent（Apache-2.0）。
    """
    try:
        return {"data": await pulse_market_pulse.fetch_overview(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"全球预期概率总览异常：{e}") from e


@app.get("/api/pulse/insight")
async def pulse_insight(module: str = Query("", max_length=32)):
    """重生成单张 AI 研判卡片。module 传「现状」刷新顶部现状长条，其余为模块名。"""
    try:
        if not module or module == "现状":
            return {"data": await pulse_market_pulse.refresh_status() or None}
        return {"data": await pulse_market_pulse.refresh_insight(module)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"AI 研判重生成异常：{e}") from e


@app.get("/api/pulse/history")
async def pulse_history(
    token_id: str = Query(..., min_length=1, max_length=128),
    interval: str = Query("1w", pattern="^(1d|1w|1m|max)$"),
):
    """单条 Polymarket 事件 Yes 概率历史（趋势图）。Kalshi 无等效简单接口。"""
    try:
        history = await pulse_market_pulse.polymarket_signals.fetch_history(
            token_id=token_id, interval=interval,
        )
        return {"data": {"history": history}}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Polymarket 概率历史异常：{e}") from e


@app.get("/api/market/macro")
def market_macro(refresh: bool = False):
    """宏观经济指标（GDP/CPI/PPI/PMI/M2/工业增加值/进出口/贸易差额/社融）。缓存 1 小时。"""
    try:
        return {"data": market.get_macro(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"宏观经济指标异常：{e}") from e


@app.get("/api/bonds/curve")
def bonds_curve(refresh: bool = False):
    """中债收益率曲线 + 期限/信用利差序列。缓存 6 小时，last-good 兜底。"""
    try:
        return {"data": bonds_layer.get_curve(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"债市曲线异常：{e}") from e


@app.get("/api/bonds/overview")
def bonds_overview(refresh: bool = False):
    """债市页聚合：曲线 / 资金利率 / 政策利率锚 / 中债指数 / 中美对照。各块独立降级。"""
    try:
        return {"data": bonds_layer.get_overview(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"债市总览异常：{e}") from e


@app.get("/api/bonds/framework")
def bonds_framework(refresh: bool = False):
    """研究框架八状态仪表盘（Macro/Policy/Funding/SupplyDemand/CurveTP/Credit/Positioning/Global）。"""
    try:
        return {"data": bonds_layer.get_framework(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"债市框架异常：{e}") from e


@app.get("/api/bonds/calc")
def bonds_calc(refresh: bool = False):
    """小型计算层：各关键期限 carry / roll / breakeven（曲线推导，确定性公式）。"""
    try:
        return {"data": bonds_layer.get_calc(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"债市计算异常：{e}") from e


@app.get("/api/bonds/positioning")
def bonds_positioning(refresh: bool = False):
    """仓位与拥挤度：国债期货四品种主力持仓/成交及近一年分位。"""
    try:
        return {"data": bonds_layer.get_positioning(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"债市仓位异常：{e}") from e


@app.get("/api/bonds/segments")
def bonds_segments(refresh: bool = False):
    """分品种评分：短债/中短/长债/超长/信用/杠杆套息，八状态加权 + carry 锚 + 失效条件。"""
    try:
        return {"data": bonds_layer.get_segments(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"债市分品种评分异常：{e}") from e


@app.get("/api/gold/score")
def gold_score(refresh: bool = False):
    """黄金价格多维评分（方案 V2.1）。缓存 30 分钟，last-good 兜底。"""
    try:
        return {"data": gold_score_layer.get_gold_score(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"黄金评分异常：{e}") from e


@app.get("/api/gold/spot")
def gold_spot():
    """实时金价：伦敦金（XAU）与纽约金（GC），20 秒缓存。"""
    try:
        return {"data": gold_score_layer.gold_spot()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"实时金价异常：{e}") from e


@app.get("/api/gold/cn-spot")
def gold_cn_spot():
    """国内金价：沪金99（AU9999）与黄金延期（AUTD），CNY/克，20 秒缓存。"""
    try:
        return {"data": gold_score_layer.cn_gold_spot()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"国内金价异常：{e}") from e


@app.get("/api/gold/au0-hist")
def gold_au0_hist(days: int = Query(400, ge=60, le=1000)):
    """沪金主力（AU0）日K收盘序列：评分卡旁国内金价近1年走势，1 小时缓存。"""
    try:
        return {"data": gold_score_layer.au0_daily_history(days)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"沪金日K异常：{e}") from e


@app.get("/api/gold/paxg")
def gold_paxg():
    """PAXG-USD 暗盘现货：7×24 实时行情 + 当日分时（Binance 公共镜像），20 秒缓存。"""
    try:
        return {"data": gold_score_layer.paxg_usd_spot()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"PAXG 暗盘异常：{e}") from e


@app.get("/api/oil/score")
def oil_score(refresh: bool = False):
    """油价多维评分（框架 V1.0）：物理稀缺/供给/炼化/仓位/溢价/美元/动量。缓存 1 小时。"""
    try:
        return {"data": oil_layer.get_oil_score(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"油价评分异常：{e}") from e


@app.get("/api/oil/spot")
def oil_spot():
    """实时油价：Brent / WTI / 天然气（腾讯 hf_），20 秒缓存。"""
    try:
        return {"data": oil_layer.oil_spot()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"实时油价异常：{e}") from e


@app.get("/api/oil/brent-hist")
def oil_brent_hist(days: int = Query(400, ge=60, le=1000)):
    """布伦特连续（OIL）日K收盘序列：评分卡旁油价近1年走势，1 小时缓存。"""
    try:
        return {"data": oil_layer.brent_daily_history(days)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"布伦特日K异常：{e}") from e


@app.get("/api/allocation")
def allocation(refresh: bool = False):
    """择时 + 大类资产配置：市场环境研判（5 档风险等级）→ 股/债/商品/现金目标权重。缓存 1 小时。"""
    try:
        return {"data": timing_alloc.get_timing_allocation(force=refresh)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"择时配置异常：{e}") from e


@app.get("/api/allocation/insight")
async def allocation_insight(refresh: bool = Query(False)):
    """AI 通俗解读当前择时分（宏观/流动性/市场确认三角度）。refresh=true 重新生成并写回快照。"""
    try:
        return {"data": await timing_alloc.get_ai_insight(force=refresh) or ""}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"择时 AI 解读异常：{e}") from e


# ---------------------------------------------------------------------------
# 因子实验室：数据构建 / 单因子检验 / 探索性组合回测
# ---------------------------------------------------------------------------

@app.get("/api/factor/lab")
def factor_lab():
    """因子实验室元信息：可用因子（分组）、数据状态（日线+PIT 财务）、偏差标签。"""
    import factor_data
    import factor_pit
    import factors

    status = factor_data.lab_status()
    status["fundamentals"] = factor_pit.pit_status()
    return {"data": {
        "factors": [{"id": k, "name": v} for k, v in factors.FACTOR_META.items()],
        "fin_factors": [{"id": k, "name": v} for k, v in factors.FIN_FACTOR_META.items()],
        **status,
    }}


def _run_full_build():
    """日线 → PIT 财务 两阶段顺序构建（共享一个后台线程）。"""
    import factor_data
    import factor_pit

    try:
        factor_data.build_dataset()
    except Exception:  # noqa: BLE001 — 日线失败也继续试财务（可能已有旧日线）
        pass
    try:
        factor_pit.build_fundamentals()
    except Exception:  # noqa: BLE001
        pass


@app.post("/api/factor/build")
def factor_build():
    """触发后台数据构建：日线（约 20-40 分钟）→ PIT 财务 2006-今（约 10-20 分钟）。"""
    import threading

    import factor_data
    import factor_pit

    # 检查+占位必须原子：否则并发双击会各起一个线程、双跑 20-40 分钟构建并双写数据文件
    with factor_data._STATE_LOCK:
        already = factor_data._STATE["building"] or factor_pit._STATE["building"]
        if not already:
            factor_data._STATE.update(building=True, pre_acquired=True)  # 占位；线程内 build_dataset 认这个标记
    if already:
        return {"data": {"building": True, "started": False}}
    threading.Thread(target=_run_full_build, daemon=True).start()
    return {"data": {"building": True, "started": True}}


@app.get("/api/factor/data-status")
def factor_data_status():
    """构建进度 / 最近一次构建摘要（日线 + 财务两段）。"""
    import factor_data
    import factor_pit

    data = factor_data.lab_status()
    data["fundamentals"] = factor_pit.pit_status()
    return {"data": data}


_factor_eval_cache: dict[str, tuple] = {}


def _cache_put(cache: dict, key: str, value, limit: int = 8) -> None:
    """结果缓存带条数上限（FIFO 淘汰），防长期运行无限膨胀。"""
    cache[key] = value
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


def _factor_input_version() -> str:
    """缓存键的数据版本：日线 catalog + 财务表 + 自定义因子公式的 mtime。

    data_version() 只看日线 catalog——财务单独重建或 custom 因子改公式时它不变，
    只用它做键会返回过期缓存。
    """
    import factor_data
    import factor_expr
    import factor_pit

    parts = [factor_data.data_version()]
    for path in (factor_pit._FUND_FILE, factor_expr._CUSTOM_FILE):
        try:
            parts.append(str(os.path.getmtime(path)))
        except OSError:
            parts.append("-")
    return "|".join(parts)


@app.get("/api/factor/evaluate")
def factor_evaluate(factor: str, start: str | None = None, end: str | None = None):
    """单因子检验（Alphalens 口径）：IC/RankIC、五分组、换手、分年。计算较重，按参数+数据版本缓存。"""
    import factors

    try:
        key = f"{factor}|{start}|{end}|{_factor_input_version()}"
        if key not in _factor_eval_cache:
            _cache_put(_factor_eval_cache, key, factors.evaluate(factor, start, end))
        return {"data": _factor_eval_cache[key]}
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    except (ValueError, KeyError) as e:
        raise HTTPException(400, f"因子检验参数异常：{e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"因子检验异常：{e}") from e


_factor_bt_cache: dict[str, tuple] = {}


@app.get("/api/factor/backtest")
def factor_backtest_endpoint(factor: str, start: str | None = None, end: str | None = None,
                             top_n: int = Query(50, ge=1, le=500), top_pct: float | None = Query(None, gt=0, le=1),
                             freq: str = "monthly", cost: float = Query(1.0, ge=0, le=3)):
    """探索性组合回测：只多等权 TopN、周/月调仓、T+1 成交，含 0/1/2/3x 成本压力。"""
    import factor_backtest

    try:
        key = f"{factor}|{start}|{end}|{top_n}|{top_pct}|{freq}|{cost}|{_factor_input_version()}"
        if key not in _factor_bt_cache:
            _cache_put(_factor_bt_cache, key, factor_backtest.run_backtest(
                factor, start, end, top_n=top_n, top_pct=top_pct, freq=freq, cost=cost))
        return {"data": _factor_bt_cache[key]}
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, f"回测参数异常：{e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"回测异常：{e}") from e


# —— 因子构建（公式引擎）：字段文档 / 静态校验 / 自定义因子 CRUD ——

class FactorSaveReq(BaseModel):
    id: str
    name: str
    expr: str


@app.get("/api/factor/fields")
def factor_fields():
    """因子公式引擎的字段与算子文档 + 已保存的自定义因子列表。"""
    import factor_expr

    return {"data": {**factor_expr.fields_doc(), "custom": factor_expr.list_custom()}}


@app.post("/api/factor/validate")
def factor_validate(req: FactorSaveReq):
    """静态校验公式（解析 + 参数检查）；expr 可以先不保存。"""
    import factor_expr

    try:
        factor_expr.compile_expr(req.expr)
        return {"data": {"ok": True}}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/factor/custom")
def factor_custom_save(req: FactorSaveReq):
    """保存自定义因子（id 唯一，重复即覆盖）。之后用 factor='custom:<id>' 检验/回测。"""
    import factor_expr

    try:
        return {"data": factor_expr.save_custom(req.id, req.name, req.expr)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/factor/custom/{fid}")
def factor_custom_delete(fid: str):
    import factor_expr

    factor_expr.delete_custom(fid)
    return {"data": {"deleted": fid}}
