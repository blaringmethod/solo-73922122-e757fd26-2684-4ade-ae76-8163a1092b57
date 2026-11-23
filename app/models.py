"""Pydantic request/response models."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

YEAR_MIN = 1
YEAR_MAX = 9999


class CoreReport(BaseModel):
    """One core's full yearly index series. Re-reporting replaces it entirely."""

    model_config = ConfigDict(extra="forbid")

    tree_no: Annotated[int, Field(ge=1, description="树号")]
    core_no: Annotated[int, Field(ge=1, description="芯号，同一树内唯一")]
    years: Annotated[
        dict[int, int],
        Field(
            description=(
                "年份 -> 整数轮宽指数。重报会整根替换：这里没有的年份会从该芯删除，"
                "该芯随即退出那些年份的计算。"
            )
        ),
    ]

    @field_validator("years", mode="before")
    @classmethod
    def _reject_non_integer_values(cls, value):
        if isinstance(value, dict):
            for year, idx in value.items():
                if isinstance(idx, bool):
                    raise ValueError(f"year {year}: index must be an integer, not a boolean")
                if isinstance(idx, float) and not idx.is_integer():
                    raise ValueError(f"year {year}: index must be a whole number")
        return value

    @field_validator("years")
    @classmethod
    def _check_years(cls, value: dict[int, int]) -> dict[int, int]:
        for year, idx in value.items():
            if not (YEAR_MIN <= year <= YEAR_MAX):
                raise ValueError(f"year {year} outside {YEAR_MIN}..{YEAR_MAX}")
            if idx < 0:
                raise ValueError(f"year {year}: index must be a non-negative integer")
        return value


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cores: list[CoreReport] = Field(
        min_length=1,
        max_length=5000,
        description="要（重）报的芯。每个芯的序列整体替换，一个请求一个事务。",
    )

    @model_validator(mode="after")
    def _unique_cores(self) -> "ReportRequest":
        seen: set[tuple[int, int]] = set()
        for core in self.cores:
            key = (core.tree_no, core.core_no)
            if key in seen:
                raise ValueError(f"core {key} appears more than once in one report")
            seen.add(key)
        return self


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    year: Annotated[int, Field(ge=YEAR_MIN, le=YEAR_MAX)]
    exclude_tree: Annotated[
        int | None,
        Field(ge=1, description="临时剔除的待核树号；只影响本次查询"),
    ] = None


class DefinitionOut(BaseModel):
    inner: str = "对入选树，先取该树当年所有已报芯指数的中位数（株内中位数）"
    outer: str = "再对所有入选树的株内中位数取中位数，得到参考值"
    rounding: str = (
        "全程以二倍/四倍整数精确计算；偶数个样本时中位数可为 .5，"
        "value_x4 是精确的 4 倍值，value = value_x4 / 4"
    )
    exclusion: str = (
        "exclude_tree 只在该次查询中剔除该树全部芯；该树当年无数据时不计入排除，"
        "样本树数按剔除后实际有数据的树计算"
    )


class ReferenceResponse(BaseModel):
    year: int
    exclude_tree: int | None = None
    trees_in_year: int = Field(description="未剔除时当年有数据的树数")
    sample_trees: int = Field(description="剔除后当年实际参与计算的树数")
    enough_samples: bool
    status: str = Field(description="'ok' 表示样本树数 >= 3，否则 'insufficient_samples'")
    min_trees: int = 3
    value_x4: int | None = Field(default=None, description="参考值的精确 4 倍（整数）")
    value: float | None = Field(default=None, description="参考值；粒度为 0.25")
    excluded_tree_present: bool = Field(
        default=False, description="exclude_tree 当年是否有数据（有则从计算中扣除）"
    )
    excluded_tree_median_x2: int | None = Field(
        default=None, description="该树当年的株内中位数（二倍整数），仅作核对"
    )
    data_version: int
    definition: DefinitionOut = Field(default_factory=DefinitionOut)
    read_cost: dict[str, int] = Field(
        description="本次查询实际向 SQLite 读取的行数/语句数/直方图探测次数"
    )


class ReportResponse(BaseModel):
    data_version: int
    trees_written: int
    cores_written: int
    years_changed: int
    measurements_written: int


class CoreSeriesOut(BaseModel):
    tree_no: int
    core_no: int
    revision: int
    updated_at: str
    years: dict[int, int]


class TreeListOut(BaseModel):
    data_version: int
    trees: list[int]


class HealthOut(BaseModel):
    status: str
    data_version: int
    schema_ok: bool
