"""几何核心与评估 API 的单元 / 集成测试。

所有期望值均手工核算：
- 场景 A（2x2 栅格 + 两组平移）验证未知像元只入上界、跨组 min/max 汇总；
- 三角形场景验证斜边真实相交面积（Sutherland–Hodgman 裁剪）；
- 其余场景验证判定分支与非法输入的整次拒绝。
"""
import pytest
from fastapi.testclient import TestClient

from geometry import clip_to_rect, is_simple_polygon, polygon_area, rect_intersection_area
from main import app

client = TestClient(app)


# ---------------------------------------------------------------- 几何原语

def test_polygon_area_axis_aligned():
    assert polygon_area([(0, 0), (10, 0), (10, 5), (0, 5)]) == pytest.approx(50.0)
    # 方向无关
    assert polygon_area([(0, 0), (0, 5), (10, 5), (10, 0)]) == pytest.approx(50.0)


def test_polygon_area_large_coordinates():
    # 高斯投影量级坐标下的 1 平方米地块
    pts = [(500000.0, 3456000.0), (500001.0, 3456000.0),
           (500001.0, 3456001.0), (500000.0, 3456001.0)]
    assert polygon_area(pts) == pytest.approx(1.0, abs=1e-6)


def test_clip_triangle_by_rect():
    tri = [(6.0, 1.0), (14.0, 1.0), (10.0, 9.0)]
    clipped = clip_to_rect(tri, float("-inf"), float("-inf"), 10.0, float("inf"))
    assert polygon_area(clipped) == pytest.approx(16.0)
    assert rect_intersection_area(tri, -1e9, -1e9, 10.0, 1e9) == pytest.approx(16.0)


def test_rect_intersection_area_partial():
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert rect_intersection_area(square, 5.0, 5.0, 15.0, 15.0) == pytest.approx(25.0)
    assert rect_intersection_area(square, 20.0, 20.0, 30.0, 30.0) == 0.0


def test_simple_polygon_checks():
    assert is_simple_polygon([(0, 0), (10, 0), (10, 10), (0, 10)], 1e-9)
    # 蝴蝶结（自相交）
    assert not is_simple_polygon([(0, 0), (10, 10), (10, 0), (0, 10)], 1e-9)
    # 重复顶点
    assert not is_simple_polygon([(0, 0), (5, 0), (0, 0), (5, 5)], 1e-9)


# ---------------------------------------------------------------- 场景构造

def scenario_a(**overrides):
    """2x2 栅格：行0(北)=[flooded, unknown]，行1=[dry, flooded]；
    地块 (10,2)-(20,12)，面积 100；平移 (0,0) 与 (2,1)。

    手算：
      t0: flooded 80, unknown 20  -> [80, 100]
      t1: flooded 2+72=74, unknown 8 -> [74, 82]
      全局: lower=74, upper=100 -> 占比 [0.74, 1.0]
    """
    payload = {
        "grid": {
            "origin": [0.0, 20.0],
            "cell_size": 10,
            "rows": [
                ["flooded", "unknown"],
                ["dry", "flooded"],
            ],
        },
        "translations": [{"dx": 0.0, "dy": 0.0}, {"dx": 2.0, "dy": 1.0}],
        "parcels": [
            {
                "id": "A-01",
                "vertices": [[10, 2], [20, 2], [20, 12], [10, 12]],
            }
        ],
        "threshold": 0.7,
    }
    payload.update(overrides)
    return payload


def test_scenario_a_confirmed():
    r = client.post("/assess", json=scenario_a())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["threshold"] == 0.7
    (p,) = body["parcels"]
    assert p["id"] == "A-01"
    assert p["area"] == pytest.approx(100.0)
    assert p["flooded_area_lower"] == pytest.approx(74.0)
    assert p["flooded_area_upper"] == pytest.approx(100.0)
    assert p["ratio_lower"] == pytest.approx(0.74)
    assert p["ratio_upper"] == pytest.approx(1.0)
    assert p["verdict"] == "confirmed"  # 0.74 >= 0.7
    # 逐组明细：未知像元只计入上界，且不跨组拼接
    details = {(d["index"]): d for d in p["translations"]}
    assert details[0]["flooded_area"] == pytest.approx(80.0)
    assert details[0]["potential_area"] == pytest.approx(100.0)
    assert details[1]["flooded_area"] == pytest.approx(74.0)
    assert details[1]["potential_area"] == pytest.approx(82.0)


def test_scenario_a_review_when_threshold_between_bounds():
    r = client.post("/assess", json=scenario_a(threshold=0.8))
    (p,) = r.json()["parcels"]
    assert p["verdict"] == "review"  # 0.74 < 0.8 <= 1.0


def test_unknown_only_counts_in_upper_bound():
    payload = {
        "grid": {"origin": [0.0, 10.0], "cell_size": 10, "rows": [["unknown"]]},
        "translations": [{"dx": 0.0, "dy": 0.0}, {"dx": 1.0, "dy": 0.0}],
        "parcels": [{"id": "U", "vertices": [[2, 2], [8, 2], [8, 8], [2, 8]]}],
        "threshold": 0.5,
    }
    r = client.post("/assess", json=payload)
    assert r.status_code == 200, r.text
    (p,) = r.json()["parcels"]
    assert p["area"] == pytest.approx(36.0)
    assert p["flooded_area_lower"] == 0.0
    assert p["flooded_area_upper"] == pytest.approx(36.0)  # 面积守恒
    assert p["ratio_lower"] == 0.0
    assert p["ratio_upper"] == pytest.approx(1.0)
    assert p["verdict"] == "review"


def test_not_met_verdict():
    payload = {
        "grid": {"origin": [0.0, 10.0], "cell_size": 10, "rows": [["dry"]]},
        "translations": [{"dx": 0.0, "dy": 0.0}, {"dx": 1.0, "dy": 0.0}],
        "parcels": [{"id": "D", "vertices": [[2, 2], [8, 2], [8, 8], [2, 8]]}],
        "threshold": 0.1,
    }
    (p,) = client.post("/assess", json=payload).json()["parcels"]
    assert p["ratio_upper"] == 0.0
    assert p["verdict"] == "not_met"


def test_zero_threshold_confirms_everything():
    payload = {
        "grid": {"origin": [0.0, 10.0], "cell_size": 10, "rows": [["dry"]]},
        "translations": [{"dx": 0.0, "dy": 0.0}, {"dx": 1.0, "dy": 0.0}],
        "parcels": [{"id": "D", "vertices": [[2, 2], [8, 2], [8, 8], [2, 8]]}],
        "threshold": 0.0,
    }
    (p,) = client.post("/assess", json=payload).json()["parcels"]
    assert p["verdict"] == "confirmed"  # 下界 0 >= 阈值 0


def test_triangle_true_intersection_area():
    """三角形 (6,1),(14,1),(10,9)，面积 32；行0=[flooded, dry]。

    t0 (0,0): 淹水列 x∈[0,10]  -> 相交面积 16
    t1 (1,0): 淹水列 x∈[1,11]  -> 相交面积 23
    全局 [16, 23] -> 占比 [0.5, 0.71875]
    """
    payload = {
        "grid": {"origin": [0.0, 10.0], "cell_size": 10,
                 "rows": [["flooded", "dry"]]},
        "translations": [{"dx": 0.0, "dy": 0.0}, {"dx": 1.0, "dy": 0.0}],
        "parcels": [{"id": "T", "vertices": [[6, 1], [14, 1], [10, 9]]}],
        "threshold": 0.5,
    }
    r = client.post("/assess", json=payload)
    assert r.status_code == 200, r.text
    (p,) = r.json()["parcels"]
    assert p["area"] == pytest.approx(32.0)
    assert p["flooded_area_lower"] == pytest.approx(16.0)
    assert p["flooded_area_upper"] == pytest.approx(23.0)
    assert p["ratio_lower"] == pytest.approx(0.5)
    assert p["ratio_upper"] == pytest.approx(0.71875)
    assert p["verdict"] == "confirmed"  # 0.5 >= 0.5

    for th, expected in ((0.6, "review"), (0.72, "not_met")):
        payload["threshold"] = th
        (p,) = client.post("/assess", json=payload).json()["parcels"]
        assert p["verdict"] == expected


def test_multiple_parcels_and_closing_ring():
    payload = scenario_a(threshold=0.7)
    # 第二个地块：闭合环写法（首尾重复），完全位于淹水像元内
    payload["parcels"].append(
        {"id": "B-02",
         "vertices": [[11, 3], [19, 3], [19, 9], [11, 9], [11, 3]]}
    )
    r = client.post("/assess", json=payload)
    assert r.status_code == 200, r.text
    a, b = r.json()["parcels"]
    assert a["id"] == "A-01"
    assert b["area"] == pytest.approx(48.0)
    # t0: 全在 flooded 像元 (1,1) 内 -> 48；t1: 跨 dry/flooded
    assert b["ratio_upper"] == pytest.approx(1.0)
    assert b["verdict"] == "confirmed"


# ---------------------------------------------------------------- 非法输入

def _expect_422(payload, fragment=""):
    r = client.post("/assess", json=payload)
    assert r.status_code == 422, r.text
    if fragment:
        assert fragment in r.text
    return r


def test_reject_self_intersecting_parcel():
    payload = scenario_a()
    payload["parcels"] = [
        {"id": "BAD", "vertices": [[4, 4], [16, 16], [16, 4], [4, 16]]}
    ]
    _expect_422(payload, "自相交")


def test_reject_degenerate_parcel():
    payload = scenario_a()
    payload["parcels"] = [
        {"id": "BAD", "vertices": [[4, 4], [8, 8], [12, 12]]}  # 共线，面积 0
    ]
    _expect_422(payload, "面积")


def test_reject_too_many_vertices():
    payload = scenario_a()
    # 17 个互异顶点（绕过闭合环去重）
    import math

    pts = [[10 + 5 * math.cos(i * 2 * math.pi / 17),
            7 + 4 * math.sin(i * 2 * math.pi / 17)] for i in range(17)]
    payload["parcels"] = [{"id": "BAD", "vertices": pts}]
    _expect_422(payload, "顶点数")


def test_reject_parcel_outside_any_candidate_grid():
    payload = scenario_a()
    # 在 t0 范围内，但 y=0.5 超出 t1 的下界 y=1
    payload["parcels"] = [
        {"id": "BAD", "vertices": [[10, 0.5], [20, 0.5], [20, 5], [10, 5]]}
    ]
    _expect_422(payload, "超出")


def test_one_bad_parcel_rejects_whole_request():
    payload = scenario_a()
    payload["parcels"].append(
        {"id": "BAD", "vertices": [[4, 4], [16, 16], [16, 4], [4, 16]]}
    )
    r = _expect_422(payload, "整次请求被拒绝")
    assert "A-01" not in r.text or "parcels" not in r.json().get("detail", {})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["grid"].update(cell_size=0),
        lambda p: p["grid"].update(cell_size=-5),
        lambda p: p["grid"].update(rows=[["flooded", "dry"], ["flooded"]]),  # 非矩形
        lambda p: p["grid"].update(rows=[["flooded"]] * 33),  # 超过 32 行
        lambda p: p["grid"].update(rows=[["flooded"] * 33]),  # 超过 32 列
        lambda p: p["grid"].update(rows=[["wet"]]),  # 非法像元值
        lambda p: p.update(translations=[{"dx": 0, "dy": 0}]),  # 少于 2 组
        lambda p: p.update(translations=[{"dx": i, "dy": 0} for i in range(5)]),  # 多于 4 组
        lambda p: p.update(parcels=[]),  # 少于 1 个地块
        lambda p: p.update(parcels=p["parcels"] * 9),  # 多于 8 个地块
        lambda p: p.update(threshold=-0.1),
        lambda p: p.update(threshold=1.5),
        lambda p: p["parcels"][0].update(vertices=[[0, 0], [1, 1]]),  # 少于 3 顶点
    ],
)
def test_schema_validation_rejects_bad_requests(mutate):
    payload = scenario_a()
    mutate(payload)
    _expect_422(payload)


def test_healthz_and_docs():
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/assess" in spec["paths"]
