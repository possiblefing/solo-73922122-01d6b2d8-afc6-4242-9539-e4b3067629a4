"""洪水受灾耕地评估 + 灾后瓦片汇交 API。

两部分能力：
1. POST /assess：分类栅格与耕地地块的受淹占比上下界评估（原有能力）；
2. /submissions：航次登记、分片上传原件、缺片/状态查询、发布与下载，
   发布后的拼接网格形状与 /assess 的 grid 入参完全一致，可直接送评估。

交互式文档见 /docs。
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from ingest_models import (
    RegisterRequest,
    SubmissionStatus,
    TileAck,
    TileUpload,
)
from models import AssessmentRequest, AssessmentResponse
from service import assess
from storage import Conflict, NotFound, Storage, Unprocessable

DATA_DIR = os.environ.get("FLOOD_DATA_DIR", "/data")
storage = Storage(DATA_DIR)
# 导入即幂等建表（TestClient 不触发 lifespan 时同样可用；重复调用无副作用）
storage.init()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建库建表 + 重启对账：确认已收原件/已发布网格在共享卷中是否齐全
    storage.init()
    try:
        report = storage.reconcile()
        if report["missing_objects"] or report["missing_grids"]:
            # 仅告警：状态机仍以发布前强制复核为准，不会产生「已发布却缺文件」
            print(f"[reconcile] 共享卷对账异常: {json.dumps(report, ensure_ascii=False)}")
    except Exception as exc:  # pragma: no cover - 对账失败不应阻断启动
        print(f"[reconcile] 对账失败（已忽略）: {exc}")
    yield


app = FastAPI(
    title="洪水受灾耕地评估与瓦片汇交 API",
    description=(
        "## 评估能力\n"
        "将洪水分类栅格（淹水/未淹/未知）按多组候选整幅平移叠加到耕地地块上，"
        "按真实相交面积计算受淹占比上下界并给出判定。\n\n"
        "## 汇交能力\n"
        "1. `PUT /submissions/{flight_id}` 登记航次（网格原点、像元边长、"
        "≤32×32 行列、≤4 片的片号/左闭右开窗口/原件 SHA-256），窗口须铺满目标；\n"
        "2. `PUT .../tiles/{tile_id}` 逐片上传 JSON 分类矩阵原件，"
        "同片同原文重试幂等复用，异内容拒绝；重叠处分类必须一致，冲突即 blocked；\n"
        "3. 未收齐或校验不符不能发布；发布后清单与瓦片不可改；\n"
        "4. 可查询缺片与状态，下载原件与拼接网格；"
        "拼接网格可直接作为 `POST /assess` 的 `grid` 字段。\n\n"
        "状态存 SQLite（WAL），原件存共享卷内容寻址存储，双实例并发安全、重启可续交。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(NotFound)
async def not_found_handler(request: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(Conflict)
async def conflict_handler(request: Request, exc: Conflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(Unprocessable)
async def unprocessable_handler(request: Request, exc: Unprocessable) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
async def pydantic_validation_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    # 端点内手工校验（上传原件矩阵）抛出的 Pydantic 错误统一为 422
    return JSONResponse(
        status_code=422,
        content={"detail": "请求体校验失败", "errors": exc.errors()},
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


if os.environ.get("FLOOD_ENABLE_SHUTDOWN_HOOK") == "1":
    import threading

    @app.post("/shutdown", include_in_schema=False)
    def shutdown_hook() -> dict:
        """测试/运维钩子：仅当 FLOOD_ENABLE_SHUTDOWN_HOOK=1 时注册。

        沙箱等禁止向子进程发信号的环境下，用它让进程自行退出，
        以验证「落盘后进程退出、重启可续交」。
        """
        def _exit() -> None:
            import time as _time

            _time.sleep(0.2)  # 先让响应发出
            os._exit(0)

        threading.Thread(target=_exit, daemon=True).start()
        return {"status": "shutting_down"}


# ---------------------------------------------------------------- 评估
@app.post(
    "/assess",
    response_model=AssessmentResponse,
    summary="计算各地块受淹占比上下界并判定",
    responses={
        200: {"description": "逐地块面积、占比上下界及判定"},
        422: {"description": "请求参数非法或存在非法地块，整次拒绝"},
    },
)
def assess_endpoint(req: AssessmentRequest) -> AssessmentResponse:
    return assess(req)


# ---------------------------------------------------------------- 汇交：登记
@app.put(
    "/submissions/{flight_id}",
    response_model=SubmissionStatus,
    summary="登记航次清单（同号同清单重试幂等复用，异内容拒绝）",
    responses={
        200: {"description": "登记成功或同清单幂等返回"},
        409: {"description": "航次已存在但清单不一致，或已发布不可修改"},
        422: {"description": "窗口越界/未铺满/片号重复等语义错误"},
    },
)
def register_submission(flight_id: str, req: RegisterRequest) -> dict:
    if flight_id != req.flight_id:
        raise Unprocessable("路径航次号与请求体 flight_id 不一致")
    payload = req.model_dump()
    status_view, created = storage.register(payload)
    status_view["reused"] = not created
    return status_view


@app.get(
    "/submissions",
    summary="列出全部航次及其状态",
)
def list_submissions() -> dict:
    return {"flights": storage.list_flights()}


@app.get(
    "/submissions/{flight_id}",
    response_model=SubmissionStatus,
    summary="查询航次状态与缺片/冲突明细",
    responses={404: {"description": "航次未登记"}},
)
def get_submission(flight_id: str) -> dict:
    view = storage.get_status(flight_id)
    view["reused"] = False
    return view


# ---------------------------------------------------------------- 汇交：上传
@app.put(
    "/submissions/{flight_id}/tiles/{tile_id}",
    response_model=TileAck,
    summary="上传一片 JSON 分类矩阵原件（失败可重传，同原文复用/异内容拒绝）",
    responses={
        200: {"description": "新收录或同原文幂等复用"},
        404: {"description": "航次或片号不存在"},
        409: {"description": "异内容重传、重叠分类冲突或已发布不可改"},
        422: {"description": "原件 SHA-256/形状/取值校验不符"},
    },
)
async def upload_tile(flight_id: str, tile_id: str, request: Request) -> dict:
    raw = await request.body()
    if not raw:
        raise Unprocessable("请求体为空，须上传 JSON 分类矩阵原件")
    # 资源存在性先于请求体校验：未知航次/片号返回 404，而非 422
    storage.get_status(flight_id)
    # 先走 Pydantic 校验（形状/取值非法由 ValidationError 处理器统一返回 422），
    # 哈希始终以请求的原始字节为准
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Unprocessable(f"原件不是合法 UTF-8 JSON：{exc}")
    TileUpload.model_validate(parsed)
    view, is_new = storage.upload_tile(flight_id, tile_id, raw)
    return TileAck(
        flight_id=flight_id,
        tile_id=tile_id,
        status=view["status"],
        content_sha256=view["content_sha256"],
        reused=not is_new,
    )


# ---------------------------------------------------------------- 汇交：发布
@app.post(
    "/submissions/{flight_id}/publish",
    response_model=SubmissionStatus,
    summary="收齐且校验通过后发布（已发布为幂等返回）",
    responses={
        200: {"description": "发布成功（或此前已发布）"},
        404: {"description": "航次未登记"},
        422: {"description": "缺片、冲突、原件缺失等，不能发布"},
    },
)
def publish_submission(flight_id: str) -> dict:
    view = storage.publish(flight_id)
    view["reused"] = False
    return view


# ---------------------------------------------------------------- 汇交：下载
@app.get(
    "/submissions/{flight_id}/tiles/{tile_id}/original",
    summary="下载某片已收录原件（原始 JSON 字节）",
    responses={
        200: {"description": "原件字节，Content-Disposition 附带文件名"},
        404: {"description": "航次/片不存在或该片尚未上传"},
    },
)
def download_original(flight_id: str, tile_id: str) -> Response:
    filename, media_type, data = storage.get_original(flight_id, tile_id)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/submissions/{flight_id}/grid",
    summary="发布后下载拼接网格（可直接作为 /assess 的 grid 入参）",
    responses={
        200: {"description": "拼接网格 {origin, cell_size, rows}"},
        404: {"description": "航次未登记或网格文件缺失"},
        409: {"description": "航次尚未发布"},
    },
)
def download_grid(flight_id: str) -> Response:
    filename, data = storage.get_grid(flight_id)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
