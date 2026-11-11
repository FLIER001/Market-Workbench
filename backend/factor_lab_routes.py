"""因子研究工作台 HTTP 路由。

- `create_router(current_user_callback)` 复用 app 的 `_current_user`，不 import app（无循环依赖）。
- 所有响应 `{data: ...}`；记录按账号隔离，引用/导出同样校验归属。
- 错误映射：400 配置/比较无效、401 未登录（由回调抛）、404 记录不可访问、409 冲突操作。

集成（supervisor 在 app.py 中；别名避开现有 factor_lab() 路由函数）：
    import factor_lab as factor_research_layer
    import factor_lab_routes
    # _current_user 定义后注册路由：
    app.include_router(factor_lab_routes.create_router(_current_user))
    # users.init_db() 后，在 ASGI 启动、接受请求前调用：
    factor_research_layer.init_db()
    factor_research_layer.start_worker()
    # lifespan finally 中清理：
    factor_research_layer.stop_worker()
    # start_worker 的遗留任务恢复只执行一次，不能逐请求或每个 worker 重复调用。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

import factor_lab
import national_team


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PeriodReq(RequestBody):
    start: str
    end: str


class ConfigReq(RequestBody):
    horizon: int
    min_days_listed: int
    development: PeriodReq
    validation: PeriodReq
    holdout: PeriodReq


class DefinitionReq(RequestBody):
    factor: str | None = None
    expr: str | None = None
    direction: str


class StudyCreateReq(RequestBody):
    name: str
    description: str = ""
    config: ConfigReq


class StudyUpdateReq(RequestBody):
    name: str | None = None
    description: str | None = None
    archived: bool | None = None
    baseline_id: str | None = None


class RunCreateReq(RequestBody):
    name: str
    definition: DefinitionReq | None = None
    phase: str | None = None
    source_run_id: str | None = None


class CompareReq(RequestBody):
    run_ids: list[str]


class BacktestReq(RequestBody):
    run_id: str
    top_n: int | None = None
    freq: str | None = None
    cost: float | None = None


class FreezeReq(RequestBody):
    run_ids: list[str]


class DecisionReq(RequestBody):
    decision: str
    note: str
    run_ids: list[str] | None = None


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError) and not isinstance(exc, KeyError):
        return HTTPException(404, str(exc))
    if isinstance(exc, factor_lab.FactorLabConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, factor_lab.FactorLabError):
        return HTTPException(400, str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(400, "输入数据不可用")
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return HTTPException(400, "请求参数无效")
    raise exc


def create_router(current_user_callback) -> APIRouter:
    class ScopedRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def scoped(request: Request):
                def authenticate():
                    uid = int(current_user_callback(request)["id"])
                    sid = request.path_params.get("sid")
                    if sid is not None and factor_lab.get_study(uid, sid) is None:
                        raise HTTPException(404, "研究不存在")
                    return uid

                # Auth and record scope precede body parsing, including malformed JSON.
                request.state.factor_lab_uid = await run_in_threadpool(authenticate)
                try:
                    return await handler(request)
                except RequestValidationError as exc:
                    raise HTTPException(400, "请求格式或字段无效") from exc

            return scoped

    router = APIRouter(prefix="/api/factor/research", tags=["factor-research"], route_class=ScopedRoute)

    def _uid(request: Request) -> int:
        return request.state.factor_lab_uid

    def _guard(fn):
        """统一错误映射；保证每条记录访问都带账号作用域。"""
        try:
            return fn()
        except HTTPException:
            raise
        except Exception as exc:
            raise _map_error(exc) from exc

    @router.get("")
    def list_studies(request: Request):
        uid = _uid(request)
        return {"data": factor_lab.list_studies(uid)}

    @router.get("/national-team/template")
    def national_team_template(request: Request):
        _uid(request)
        return {"data": national_team.template()}

    @router.post("/national-team/evaluate")
    def national_team_evaluate(req: national_team.NationalTeamRequest, request: Request):
        _uid(request)
        return {"data": _guard(lambda: national_team.evaluate(req))}

    @router.post("")
    def create_study(req: StudyCreateReq, request: Request):
        uid = _uid(request)
        return {"data": _guard(lambda: factor_lab.create_study(uid, req.name, req.description, req.config.model_dump()))}

    @router.get("/{sid}")
    def get_study(sid: str, request: Request):
        uid = _uid(request)
        payload = factor_lab.study_payload(uid, sid)
        if payload is None:
            raise HTTPException(404, "研究不存在")
        return {"data": payload}

    @router.put("/{sid}")
    def update_study(sid: str, req: StudyUpdateReq, request: Request):
        uid = _uid(request)
        provided = req.model_dump(exclude_unset=True)
        if any(value is None for key, value in provided.items() if key != "baseline_id"):
            raise HTTPException(400, "修改字段不能为空")

        def _do():
            # 只有显式给出的字段才参与更新：baseline_id=null 是「清除基线」，
            # 字段缺省是「不动」。
            kwargs = {k: getattr(req, k) for k in
                      ("name", "description", "archived", "baseline_id") if k in provided}
            return factor_lab.update_study(uid, sid, **kwargs)

        return {"data": _guard(_do)}

    @router.post("/{sid}/runs")
    def create_run(sid: str, req: RunCreateReq, request: Request):
        uid = _uid(request)

        def _do():
            definition = req.definition.model_dump(exclude_unset=True) if req.definition is not None else None
            run = factor_lab.create_run(uid, sid, name=req.name, definition=definition,
                                        phase=req.phase, source_run_id=req.source_run_id)
            factor_lab.submit(run["id"])
            return run

        return {"data": _guard(_do)}

    @router.get("/{sid}/runs/{run_id}")
    def get_run(sid: str, run_id: str, request: Request):
        uid = _uid(request)
        run = factor_lab.get_run(uid, sid, run_id)
        if run is None:
            raise HTTPException(404, "实验不存在")
        return {"data": run}

    @router.post("/{sid}/runs/{run_id}/cancel")
    def cancel_run(sid: str, run_id: str, request: Request):
        uid = _uid(request)
        run = factor_lab.cancel_run(uid, sid, run_id)
        if run is None:
            raise HTTPException(404, "实验不存在")
        return {"data": run}

    @router.post("/{sid}/compare")
    def create_compare(sid: str, req: CompareReq, request: Request):
        uid = _uid(request)

        def _do():
            run = factor_lab.create_compare(uid, sid, "对比", req.run_ids)
            factor_lab.submit(run["id"])
            return run

        return {"data": _guard(_do)}

    @router.post("/{sid}/backtest")
    def create_backtest(sid: str, req: BacktestReq, request: Request):
        uid = _uid(request)

        def _do():
            run = factor_lab.create_backtest(uid, sid, "回测", req.run_id,
                                             top_n=req.top_n, freq=req.freq, cost=req.cost)
            factor_lab.submit(run["id"])
            return run

        return {"data": _guard(_do)}

    @router.post("/{sid}/freeze")
    def freeze(sid: str, req: FreezeReq, request: Request):
        uid = _uid(request)
        return {"data": _guard(lambda: factor_lab.freeze_study(uid, sid, req.run_ids))}

    @router.post("/{sid}/decisions")
    def add_decision(sid: str, req: DecisionReq, request: Request):
        uid = _uid(request)
        return {"data": _guard(lambda: factor_lab.add_decision(
            uid, sid, req.decision, req.note, req.run_ids))}

    @router.get("/{sid}/export")
    def export_study(sid: str, request: Request):
        uid = _uid(request)
        payload = factor_lab.export_study(uid, sid)
        if payload is None:
            raise HTTPException(404, "研究不存在")
        return {"data": payload}

    return router
