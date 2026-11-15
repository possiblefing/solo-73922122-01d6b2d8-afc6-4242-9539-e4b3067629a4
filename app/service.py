"""评估核心：逐平移组计算各地块受淹面积上下界并汇总判定。

规则（与需求一一对应）：
- 每组平移独立计算：下界 = 地块与淹水像元真实相交面积之和；
  上界 = 下界 + 地块与未知像元相交面积之和（未知像元只计入上界）；
- 全局下界 = 各组下界的最小值，全局上界 = 各组上界的最大值；
  不跨平移组拼接像元；
- 下界占比 ≥ 阈值 → 确定达标；上界占比 < 阈值 → 确定未达；其余 → 待核；
- 任一地块非法（非简单多边形、顶点数越界、面积过小、
  或超出任一候选网格范围）→ 整次请求以 422 拒绝。
"""
from __future__ import annotations

from math import floor

from fastapi import HTTPException

from geometry import Point, is_simple_polygon, polygon_area, rect_intersection_area
from models import (
    AssessmentRequest,
    AssessmentResponse,
    CellValue,
    ParcelResult,
    TranslationDetail,
    Verdict,
)

# 判定占比比较容差（吸收浮点噪声）
_RATIO_EPS = 1e-9
# 简单多边形校验的坐标容差相对量级
_GEOM_EPS_REL = 1e-9
# 最小地块面积（平方米），防止退化多边形
_MIN_AREA = 1e-9


def _normalize_vertices(raw: list[list[float]]) -> list[Point]:
    """转为点列；若首尾点完全相同（闭合环写法）则去掉末点。"""
    verts = [(float(p[0]), float(p[1])) for p in raw]
    if len(verts) >= 2 and verts[0] == verts[-1]:
        verts = verts[:-1]
    return verts


def _geom_eps(verts: list[Point]) -> float:
    scale = max(1.0, max(max(abs(x), abs(y)) for x, y in verts))
    return _GEOM_EPS_REL * scale


def _validate_parcel(parcel_id: str, verts: list[Point]) -> list[str]:
    """返回该地块的非法原因列表（空列表表示合法）。"""
    reasons: list[str] = []
    n = len(verts)
    if not (3 <= n <= 16):
        reasons.append(f"顶点数 {n} 不在 3~16 范围内")
        return reasons  # 顶点数非法时不再做后续几何检查
    eps = _geom_eps(verts)
    area = polygon_area(verts)
    if area <= _MIN_AREA:
        reasons.append("多边形面积为零或退化")
    if not is_simple_polygon(verts, eps):
        reasons.append("多边形存在重复顶点或边自相交，不是简单多边形")
    return reasons


def _check_containment(
    parcel_id: str,
    verts: list[Point],
    req: AssessmentRequest,
    ncols: int,
    nrows: int,
) -> list[str]:
    """地块须位于所有候选平移后的网格范围内（范围为凸矩形，逐顶点检查即可）。"""
    reasons: list[str] = []
    ox, oy = req.grid.origin
    cs = req.grid.cell_size
    eps = _geom_eps(verts)
    width = ncols * cs
    height = nrows * cs
    for idx, t in enumerate(req.translations):
        xmin = ox + t.dx
        xmax = xmin + width
        ymax = oy + t.dy
        ymin = ymax - height
        for x, y in verts:
            if not (
                xmin - eps <= x <= xmax + eps and ymin - eps <= y <= ymax + eps
            ):
                reasons.append(
                    f"顶点 ({x}, {y}) 超出第 {idx} 组平移后的网格范围 "
                    f"x∈[{xmin}, {xmax}], y∈[{ymin}, {ymax}]"
                )
                break  # 每组平移报告一次即可
    return reasons


def _flooded_unknown_area(
    verts: list[Point],
    rows: list[list[CellValue]],
    ox: float,
    oy: float,
    cs: int,
) -> tuple[float, float]:
    """单组平移下，地块与淹水像元、未知像元的真实相交面积。

    网格原点 (ox, oy) 为左上角，rows[0] 为最北一行。
    返回 (淹水相交面积, 未知相交面积)。
    """
    nrows = len(rows)
    ncols = len(rows[0])
    minx = min(x for x, _ in verts)
    maxx = max(x for x, _ in verts)
    miny = min(y for _, y in verts)
    maxy = max(y for _, y in verts)

    c0 = max(0, min(ncols - 1, floor((minx - ox) / cs)))
    c1 = max(0, min(ncols - 1, floor((maxx - ox) / cs)))
    r0 = max(0, min(nrows - 1, floor((oy - maxy) / cs)))
    r1 = max(0, min(nrows - 1, floor((oy - miny) / cs)))

    flooded = 0.0
    unknown = 0.0
    for r in range(r0, r1 + 1):
        ytop = oy - r * cs
        ybot = ytop - cs
        if ytop < miny or ybot > maxy:
            continue
        row = rows[r]
        for c in range(c0, c1 + 1):
            xl = ox + c * cs
            xr = xl + cs
            if xr < minx or xl > maxx:
                continue
            value = row[c]
            if value is CellValue.dry:
                continue
            area = rect_intersection_area(verts, xl, ybot, xr, ytop)
            if area <= 0.0:
                continue
            if value is CellValue.flooded:
                flooded += area
            else:  # unknown 只计入上界
                unknown += area
    return flooded, unknown


def _verdict(ratio_lower: float, ratio_upper: float, threshold: float) -> Verdict:
    if ratio_lower >= threshold - _RATIO_EPS:
        return Verdict.confirmed
    if ratio_upper < threshold - _RATIO_EPS:
        return Verdict.not_met
    return Verdict.review


def assess(req: AssessmentRequest) -> AssessmentResponse:
    rows = req.grid.rows
    nrows = len(rows)
    ncols = len(rows[0])
    ox, oy = req.grid.origin
    cs = req.grid.cell_size

    # 第一阶段：校验全部地块，任一非法则整次拒绝
    normalized: list[tuple[str, list[Point]]] = []
    errors: list[dict] = []
    for parcel in req.parcels:
        verts = _normalize_vertices(parcel.vertices)
        reasons = _validate_parcel(parcel.id, verts)
        if not reasons:
            reasons = _check_containment(parcel.id, verts, req, ncols, nrows)
        if reasons:
            errors.append({"parcel_id": parcel.id, "reasons": reasons})
        else:
            normalized.append((parcel.id, verts))
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "存在非法地块，整次请求被拒绝", "parcels": errors},
        )

    # 第二阶段：逐地块、逐平移组计算
    results: list[ParcelResult] = []
    for parcel_id, verts in normalized:
        area = polygon_area(verts)
        details: list[TranslationDetail] = []
        lowers: list[float] = []
        uppers: list[float] = []
        for idx, t in enumerate(req.translations):
            flooded, unknown = _flooded_unknown_area(
                verts, rows, ox + t.dx, oy + t.dy, cs
            )
            lower_k = flooded
            upper_k = flooded + unknown
            lowers.append(lower_k)
            uppers.append(upper_k)
            details.append(
                TranslationDetail(
                    index=idx,
                    dx=t.dx,
                    dy=t.dy,
                    flooded_area=round(lower_k, 6),
                    potential_area=round(upper_k, 6),
                )
            )
        lower = min(lowers)  # 全局下界 = 各组下界最小值
        upper = max(uppers)  # 全局上界 = 各组上界最大值
        ratio_lower = lower / area
        ratio_upper = upper / area
        results.append(
            ParcelResult(
                id=parcel_id,
                area=round(area, 6),
                flooded_area_lower=round(lower, 6),
                flooded_area_upper=round(upper, 6),
                ratio_lower=round(ratio_lower, 6),
                ratio_upper=round(ratio_upper, 6),
                verdict=_verdict(ratio_lower, ratio_upper, req.threshold),
                translations=details,
            )
        )
    return AssessmentResponse(threshold=req.threshold, parcels=results)
