"""洪水受灾耕地评估 API。

灾后核损场景：将洪水分类栅格叠加到耕地地块上，
在候选整幅平移下计算各地块受淹占比的上下界并给出判定，
筛出需要现场复核的地块。

交互式文档见 /docs。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from models import AssessmentRequest, AssessmentResponse
from service import assess

app = FastAPI(
    title="洪水受灾耕地评估 API",
    description=(
        "将洪水分类栅格（淹水/未淹/未知）按多组候选整幅平移叠加到耕地地块上，"
        "按真实相交面积计算受淹占比上下界：\n\n"
        "- 每组平移独立计算，未知像元只计入该组上界；\n"
        "- 全局下界取各组最小值、上界取各组最大值（不跨组拼接像元）；\n"
        "- 下界占比 ≥ 阈值 → `confirmed`（确定达标）；"
        "上界占比 < 阈值 → `not_met`（确定未达）；其余 → `review`（待核）；\n"
        "- 任一地块非法（非简单多边形、顶点数越界、超出任一候选网格等）"
        "则整次请求以 422 拒绝。\n\n"
        "坐标约定：栅格原点为整幅网格左上角（西北角）米制坐标，"
        "rows[0] 为最北一行，y 轴向北为正。"
    ),
    version="1.0.0",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


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
