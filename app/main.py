"""FastAPI application."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from . import __version__
from .chronology import (
    apply_report,
    get_core_series,
    list_trees,
    query_common_sample,
    query_reference,
)
from .db import init_db, read_data_version, read_snapshot
from .models import (
    CommonSampleRequest,
    CommonSampleResponse,
    CoreSeriesOut,
    HealthOut,
    QueryRequest,
    ReferenceResponse,
    ReportRequest,
    ReportResponse,
    TreeListOut,
)
from .settings import settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(settings.db_path)
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description=(
        "按树号、芯号报送逐年整数轮宽指数，并按年份查询树轮年表参考值。\n\n"
        "参考值算法：先对每棵树当年各芯指数取中位数，再对入选树的株内中位数取中位数。\n\n"
        "查询可选 `exclude_tree` 临时剔除一棵待核树，不读取原始取芯明细，"
        "只访问按年聚合的累计直方图（访问记录数随样本树数次线性增长）。\n\n"
        "`/common-sample` 用同一批树比较历年轮宽：给定年份范围和最低样本树数，"
        "返回该范围内每年都有至少这么多棵树有值的最长连续年段、完整覆盖该段的全部树号"
        "和所用数据版号。它只读连续覆盖区间索引 `tree_span`，不逐年展开，"
        "工作内存随最低样本树数增长。\n\n"
        "报送与年表在同一事务内生效；任何失败都回滚到报送前的状态。"
    ),
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthOut, tags=["运维"])
def health() -> HealthOut:
    try:
        with read_snapshot(settings.db_path, restricted=False) as session:
            row = session.fetchone(
                "SELECT COUNT(*) AS n FROM sqlite_master "
                "WHERE type='table' AND name IN "
                "('year_stat','tree_year','year_value_cum','tree_span')"
            )
            schema_ok = row is not None and int(row["n"]) == 4
            legacy = session.fetchone(
                "SELECT value FROM meta WHERE key='span_index_version'"
            )
            span_ok = legacy is not None
        version = read_data_version(settings.db_path)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    ok = schema_ok and span_ok
    return HealthOut(
        status="ok" if ok else "degraded", data_version=version, schema_ok=schema_ok
    )


@app.post(
    "/reports",
    response_model=ReportResponse,
    status_code=201,
    tags=["取芯与报送"],
    summary="报送/重报取芯逐年指数（整根替换，原子生效）",
)
def report(payload: ReportRequest) -> ReportResponse:
    try:
        result = apply_report(str(settings.db_path), payload)
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"report failed, nothing changed: {exc}") from exc
    return ReportResponse(**result)


@app.get(
    "/trees",
    response_model=TreeListOut,
    tags=["取芯与报送"],
    summary="列出已报送的树号",
)
def trees() -> TreeListOut:
    return TreeListOut(data_version=read_data_version(settings.db_path), trees=list_trees(str(settings.db_path)))


@app.get(
    "/cores/{tree_no}/{core_no}",
    response_model=CoreSeriesOut,
    tags=["取芯与报送"],
    summary="查看单根芯当前已提交的序列",
)
def core_series(
    tree_no: int = Path(ge=1),
    core_no: int = Path(ge=1),
) -> CoreSeriesOut:
    data = get_core_series(str(settings.db_path), tree_no, core_no)
    if data is None:
        raise HTTPException(status_code=404, detail=f"core {tree_no}/{core_no} not reported")
    return CoreSeriesOut(**data)


@app.get(
    "/chronology/{year}",
    response_model=ReferenceResponse,
    tags=["年表查询"],
    summary="查询某年参考值，可临时剔除一棵待核树",
)
def chronology(
    year: int = Path(ge=1, le=9999, description="年份"),
    exclude_tree: int | None = Query(
        default=None, ge=1, description="临时剔除的树号（只影响本次查询）"
    ),
) -> ReferenceResponse:
    try:
        result = query_reference(str(settings.db_path), year, exclude_tree)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=f"query failed: {exc}") from exc
    return ReferenceResponse(**result)


@app.post(
    "/chronology/query",
    response_model=ReferenceResponse,
    tags=["年表查询"],
    summary="同上（JSON 请求体版本，便于脚本）",
)
def chronology_query(payload: QueryRequest) -> ReferenceResponse:
    return chronology(year=payload.year, exclude_tree=payload.exclude_tree)


@app.get(
    "/common-sample",
    response_model=CommonSampleResponse,
    tags=["共同样本"],
    summary="查询给定年份范围内、满足最低样本树数的最长连续年段",
    responses={
        404: {
            "model": CommonSampleResponse,
            "description": "范围内不存在满足条件的年段（status=no_solution）",
        }
    },
)
def common_sample(
    start_year: int = Query(ge=1, le=9999, description="起始年（含）"),
    end_year: int = Query(ge=1, le=9999, description="结束年（含）"),
    min_trees: int = Query(ge=1, description="最低样本树数"),
):
    """在 ``[start_year, end_year]`` 内找最长连续年段，每年至少有 ``min_trees`` 棵树有值。

    只读连续覆盖区间索引与数据版号：不逐年展开区间，工作内存随 ``min_trees`` 增长。
    无解时仍返回完整结构，`status=no_solution`、`found=false`（HTTP 404）。
    """
    if start_year > end_year:
        raise HTTPException(status_code=422, detail="start_year must be <= end_year")
    try:
        result = query_common_sample(
            str(settings.db_path), start_year, end_year, min_trees
        )
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=f"query failed: {exc}") from exc
    body = CommonSampleResponse(**result)
    if not result["found"]:
        return JSONResponse(status_code=404, content=body.model_dump())
    return body


@app.post(
    "/common-sample",
    response_model=CommonSampleResponse,
    tags=["共同样本"],
    summary="同上（JSON 请求体版本，便于脚本）",
    responses={
        404: {
            "model": CommonSampleResponse,
            "description": "范围内不存在满足条件的年段（status=no_solution）",
        }
    },
)
def common_sample_post(payload: CommonSampleRequest):
    try:
        result = query_common_sample(
            str(settings.db_path),
            payload.start_year,
            payload.end_year,
            payload.min_trees,
        )
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=f"query failed: {exc}") from exc
    body = CommonSampleResponse(**result)
    if not result["found"]:
        return JSONResponse(status_code=404, content=body.model_dump())
    return body
