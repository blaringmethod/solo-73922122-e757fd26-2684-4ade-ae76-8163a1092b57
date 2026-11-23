"""FastAPI application."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Query

from . import __version__
from .chronology import (
    apply_report,
    get_core_series,
    list_trees,
    query_reference,
)
from .db import init_db, read_data_version, read_snapshot
from .models import (
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
                "WHERE type='table' AND name IN ('year_stat','tree_year','year_value_cum')"
            )
            schema_ok = row is not None and int(row["n"]) == 3
        version = read_data_version(settings.db_path)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return HealthOut(status="ok" if schema_ok else "degraded", data_version=version, schema_ok=schema_ok)


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
