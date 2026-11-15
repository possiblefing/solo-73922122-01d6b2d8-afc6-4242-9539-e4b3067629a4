"""请求 / 响应模型（Pydantic v2）。

坐标约定（务必阅读）：
- 栅格原点 origin 为**整幅网格左上角（西北角）**的米制坐标；
- rows[0] 为**最北一行**，行内自西向东排列；
- 像元 (r, c) 覆盖 x ∈ [ox + c·s, ox + (c+1)·s]，y ∈ [oy − (r+1)·s, oy − r·s]；
- 每组平移 (dx, dy) 作用于整幅网格：平移后原点为 (ox+dx, oy+dy)。
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 有限浮点数（拒绝 NaN / Inf）
Finite = Annotated[float, Field(allow_inf_nan=False)]
# 二维点 [x, y]
Point2D = Annotated[list[Finite], Field(min_length=2, max_length=2)]


class CellValue(str, Enum):
    """像元取值：flooded=淹水，dry=未淹，unknown=未知。"""

    flooded = "flooded"
    dry = "dry"
    unknown = "unknown"


class GridSpec(BaseModel):
    """米制淹水分类栅格。"""

    origin: Point2D = Field(description="网格左上角（西北角）米制坐标 [x, y]")
    cell_size: int = Field(gt=0, description="像元边长（米），正整数")
    rows: list[list[CellValue]] = Field(
        min_length=1,
        max_length=32,
        description="分类像元，rows[0] 为最北一行；行数、列数均 ≤ 32，且各行等长",
    )

    @model_validator(mode="after")
    def _check_rectangular(self) -> "GridSpec":
        width = len(self.rows[0])
        if width == 0 or width > 32:
            raise ValueError("每行像元数须在 1~32 之间")
        if any(len(r) != width for r in self.rows):
            raise ValueError("栅格各行长度必须一致（矩形栅格）")
        return self


class Translation(BaseModel):
    """一组可能的整幅平移量（米）。"""

    dx: Finite = Field(description="x 方向平移量（米）")
    dy: Finite = Field(description="y 方向平移量（米）")


class Parcel(BaseModel):
    """地块多边形：无洞简单多边形，3~16 个顶点。

    顶点按顺时针或逆时针给出；首尾顶点可重复（闭合环），重复时将自动去除。
    """

    id: str = Field(min_length=1, max_length=64, description="地块标识")
    vertices: list[Point2D] = Field(
        min_length=3, max_length=17, description="顶点 [[x,y],...]，去重后 3~16 个"
    )


class AssessmentRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "grid": {
                        "origin": [500000.0, 3456020.0],
                        "cell_size": 10,
                        "rows": [
                            ["flooded", "unknown"],
                            ["dry", "flooded"],
                        ],
                    },
                    "translations": [
                        {"dx": 0.0, "dy": 0.0},
                        {"dx": 2.0, "dy": 1.0},
                    ],
                    "parcels": [
                        {
                            "id": "A-01",
                            "vertices": [
                                [500010.0, 3456000.0],
                                [500020.0, 3456000.0],
                                [500020.0, 3456010.0],
                                [500010.0, 3456010.0],
                            ],
                        }
                    ],
                    "threshold": 0.5,
                }
            ]
        }
    )

    grid: GridSpec
    translations: list[Translation] = Field(
        min_length=2, max_length=4, description="2~4 组候选整幅平移量"
    )
    parcels: list[Parcel] = Field(min_length=1, max_length=8, description="1~8 个地块")
    threshold: float = Field(
        ge=0.0, le=1.0, allow_inf_nan=False, description="受淹占比阈值，0~1"
    )


class Verdict(str, Enum):
    """判定：confirmed=确定达标，not_met=确定未达，review=待核。"""

    confirmed = "confirmed"
    not_met = "not_met"
    review = "review"


class TranslationDetail(BaseModel):
    """单组平移下的地块受淹面积明细（不跨组拼接像元）。"""

    index: int = Field(description="平移组序号（与请求 translations 顺序一致）")
    dx: float
    dy: float
    flooded_area: float = Field(description="该组下淹水像元相交面积（下界，平方米）")
    potential_area: float = Field(
        description="该组下淹水+未知像元相交面积（上界，平方米）"
    )


class ParcelResult(BaseModel):
    id: str
    area: float = Field(description="地块面积（平方米）")
    flooded_area_lower: float = Field(description="受淹面积全局下界 = 各组下界最小值")
    flooded_area_upper: float = Field(description="受淹面积全局上界 = 各组上界最大值")
    ratio_lower: float = Field(description="受淹占比下界 = 下界 / 地块面积")
    ratio_upper: float = Field(description="受淹占比上界 = 上界 / 地块面积")
    verdict: Verdict = Field(
        description="confirmed=确定达标 / not_met=确定未达 / review=待核"
    )
    translations: list[TranslationDetail] = Field(description="逐平移组面积明细")


class AssessmentResponse(BaseModel):
    threshold: float
    parcels: list[ParcelResult]
