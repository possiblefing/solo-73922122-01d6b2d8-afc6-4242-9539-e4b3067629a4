"""平面几何原语。

提供：
- 鞋带公式多边形面积（局部坐标系下计算，降低大坐标浮点误差）；
- 简单多边形（无自交、无重复顶点）校验；
- Sutherland–Hodgman 轴对齐矩形裁剪，求多边形与像元的真实相交面积。

所有坐标均为米制平面坐标，不依赖任何第三方库。
"""
from __future__ import annotations

from typing import Sequence

Point = tuple[float, float]


def _shifted_signed_area(vertices: Sequence[Point], ref: Point) -> float:
    """以 ref 为局部原点的鞋带公式有向面积。

    米制坐标（如高斯投影带内 5e5 量级）直接相乘会损失精度，
    先平移到局部坐标再求和可把绝对误差控制在 1e-6 平方米量级。
    """
    ox, oy = ref
    acc = 0.0
    n = len(vertices)
    for i in range(n):
        x1 = vertices[i][0] - ox
        y1 = vertices[i][1] - oy
        x2 = vertices[(i + 1) % n][0] - ox
        y2 = vertices[(i + 1) % n][1] - oy
        acc += x1 * y2 - x2 * y1
    return acc / 2.0


def polygon_area(vertices: Sequence[Point]) -> float:
    """多边形面积（平方米）。"""
    return abs(_shifted_signed_area(vertices, vertices[0]))


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point, b: Point, p: Point, eps: float) -> bool:
    return (
        min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def _segments_touch_or_cross(p1: Point, p2: Point, p3: Point, p4: Point, eps: float) -> bool:
    """两线段是否相交（含端点触碰）。eps 为坐标量级容差。"""
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    eps2 = eps * eps  # 叉积量纲为坐标平方
    if ((d1 > eps2 and d2 < -eps2) or (d1 < -eps2 and d2 > eps2)) and (
        (d3 > eps2 and d4 < -eps2) or (d3 < -eps2 and d4 > eps2)
    ):
        return True
    if abs(d1) <= eps2 and _on_segment(p3, p4, p1, eps):
        return True
    if abs(d2) <= eps2 and _on_segment(p3, p4, p2, eps):
        return True
    if abs(d3) <= eps2 and _on_segment(p1, p2, p3, eps):
        return True
    if abs(d4) <= eps2 and _on_segment(p1, p2, p4, eps):
        return True
    return False


def is_simple_polygon(vertices: Sequence[Point], eps: float) -> bool:
    """校验是否为简单多边形：无重复顶点、非相邻边不得相交或触碰。"""
    n = len(vertices)
    # 重复顶点（含零长度边）
    for i in range(n):
        xi, yi = vertices[i]
        for j in range(i + 1, n):
            if abs(xi - vertices[j][0]) <= eps and abs(yi - vertices[j][1]) <= eps:
                return False
    # 非相邻边相交检查
    for i in range(n):
        a1, a2 = vertices[i], vertices[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i + 1:
                continue  # 相邻边共享顶点
            if i == 0 and j == n - 1:
                continue  # 首尾边相邻
            b1, b2 = vertices[j], vertices[(j + 1) % n]
            if _segments_touch_or_cross(a1, a2, b1, b2, eps):
                return False
    return True


def _clip_edge(
    poly: list[Point],
    inside,
    intersect,
) -> list[Point]:
    if not poly:
        return []
    out: list[Point] = []
    prev = poly[-1]
    prev_in = inside(prev)
    for cur in poly:
        cur_in = inside(cur)
        if cur_in:
            if not prev_in:
                out.append(intersect(prev, cur))
            out.append(cur)
        elif prev_in:
            out.append(intersect(prev, cur))
        prev, prev_in = cur, cur_in
    return out


def clip_to_rect(
    vertices: Sequence[Point], xmin: float, ymin: float, xmax: float, ymax: float
) -> list[Point]:
    """Sutherland–Hodgman：用轴对齐矩形裁剪多边形，返回裁剪后的顶点序列。"""

    def ix(a: Point, b: Point, x: float) -> Point:
        t = (x - a[0]) / (b[0] - a[0])
        return (x, a[1] + t * (b[1] - a[1]))

    def iy(a: Point, b: Point, y: float) -> Point:
        t = (y - a[1]) / (b[1] - a[1])
        return (a[0] + t * (b[0] - a[0]), y)

    poly = list(vertices)
    poly = _clip_edge(poly, lambda p: p[0] >= xmin, lambda a, b: ix(a, b, xmin))
    poly = _clip_edge(poly, lambda p: p[0] <= xmax, lambda a, b: ix(a, b, xmax))
    poly = _clip_edge(poly, lambda p: p[1] >= ymin, lambda a, b: iy(a, b, ymin))
    poly = _clip_edge(poly, lambda p: p[1] <= ymax, lambda a, b: iy(a, b, ymax))
    return poly


def rect_intersection_area(
    vertices: Sequence[Point], xmin: float, ymin: float, xmax: float, ymax: float
) -> float:
    """多边形与轴对齐矩形的真实相交面积（平方米）。"""
    clipped = clip_to_rect(vertices, xmin, ymin, xmax, ymax)
    if len(clipped) < 3:
        return 0.0
    # 以裁剪后多边形首顶点为局部原点，避免参考点过远导致鞋带公式抵消
    return abs(_shifted_signed_area(clipped, clipped[0]))
