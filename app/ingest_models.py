"""汇交 API 请求 / 响应模型（Pydantic v2）。

窗口约定与 /assess 的栅格一致：行 r 自北向南、列 c 自西向东，
窗口为左闭右开整数区间 [row_start, row_end) × [col_start, col_end)。
"""
from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from models import CellValue, Point2D

# 航次号 / 片号：安全字符集，杜绝路径穿越，长度 1~64
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

FlightId = Annotated[str, Field(min_length=1, max_length=64)]
TileId = Annotated[str, Field(min_length=1, max_length=64)]


def _check_id(value: str, label: str) -> str:
    if not _ID_PATTERN.match(value):
        raise ValueError(
            f"{label} 仅允许字母、数字、点、下划线、连字符（1~64 位）"
        )
    return value


class TileSpec(BaseModel):
    """登记清单中的一片：片号 + 左闭右开窗口 + 原件 SHA-256。"""

    tile_id: TileId = Field(description="片号，同航次内唯一")
    row_start: int = Field(ge=0, description="窗口起始行（含）")
    row_end: int = Field(ge=1, description="窗口结束行（不含）")
    col_start: int = Field(ge=0, description="窗口起始列（含）")
    col_end: int = Field(ge=1, description="窗口结束列（不含）")
    expected_sha256: str = Field(
        description="该片原件（JSON 分类矩阵原始字节）的 SHA-256（十六进制）"
    )

    @field_validator("tile_id")
    @classmethod
    def _tile_id_safe(cls, v: str) -> str:
        return _check_id(v, "片号")

    @field_validator("expected_sha256")
    @classmethod
    def _sha_format(cls, v: str) -> str:
        if not _SHA256_PATTERN.match(v):
            raise ValueError("SHA-256 须为 64 位十六进制字符串")
        return v.lower()


class RegisterRequest(BaseModel):
    """登记航次：网格参数 + 至多 4 片的清单。"""

    flight_id: FlightId = Field(description="航次号，全局唯一标识")
    origin: Point2D = Field(description="网格左上角（西北角）米制坐标 [x, y]")
    cell_size: int = Field(gt=0, description="像元边长（米），正整数")
    grid_rows: int = Field(ge=1, le=32, description="目标网格行数（1~32）")
    grid_cols: int = Field(ge=1, le=32, description="目标网格列数（1~32）")
    tiles: list[TileSpec] = Field(
        min_length=1, max_length=4, description="1~4 片，窗口须铺满目标网格"
    )

    @field_validator("flight_id")
    @classmethod
    def _flight_id_safe(cls, v: str) -> str:
        return _check_id(v, "航次号")


class TileUpload(BaseModel):
    """片上传请求体：JSON 分类矩阵，沿用 flooded/dry/unknown 三类值。"""

    rows: list[list[CellValue]] = Field(
        min_length=1,
        max_length=32,
        description="分类矩阵，形状须与该片登记窗口一致；值沿用三类分类",
    )

    @field_validator("rows")
    @classmethod
    def _rectangular(cls, v: list[list[CellValue]]) -> list[list[CellValue]]:
        width = len(v[0])
        if width == 0 or width > 32:
            raise ValueError("每行像元数须在 1~32 之间")
        if any(len(r) != width for r in v):
            raise ValueError("矩阵各行长度必须一致（矩形）")
        return v


class TileStatus(BaseModel):
    tile_id: str
    row_start: int
    row_end: int
    col_start: int
    col_end: int
    expected_sha256: str
    status: str = Field(
        description="pending=待上传 / received=已收录 / conflict=重叠冲突"
    )
    content_sha256: str | None = Field(
        default=None, description="实际收录原件的 SHA-256（未上传时为 null）"
    )


class SubmissionStatus(BaseModel):
    flight_id: str
    status: str = Field(
        description="collecting=收集中 / blocked=校验冲突禁止发布 / published=已发布"
    )
    origin: list[float]
    cell_size: int
    grid_rows: int
    grid_cols: int
    tile_count: int
    received_count: int
    missing_tiles: list[str] = Field(description="缺片片号列表")
    conflict_tiles: list[str] = Field(description="存在重叠分类冲突的片号")
    grid_object_sha256: str | None = None
    published_at: str | None = None
    tiles: list[TileStatus]
    reused: bool = Field(
        default=False, description="true=同号同清单重试命中既有登记"
    )


class TileAck(BaseModel):
    flight_id: str
    tile_id: str
    status: str
    content_sha256: str
    reused: bool = Field(description="true=同原文重试幂等复用，false=本次新收录")
