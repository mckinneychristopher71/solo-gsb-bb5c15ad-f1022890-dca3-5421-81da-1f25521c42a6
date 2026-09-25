"""FastAPI application: web UI + REST API for standard-address matching."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    address_audit,
    consensus,
    coverage_gaps,
    db,
    experiments,
    import_impact,
    ingest,
    review_groups,
    review_replay,
)
from .config import Settings, get_settings
from .matcher import candidate_to_dict, match_address, result_to_dict
from .schemas import (
    AddressBatchIn,
    BatchMatchRequest,
    ExperimentCompareRequest,
    ExperimentRequest,
    GroupReviewAction,
    MatchRequest,
    ReviewAction,
)

BASE_DIR = Path(__file__).resolve().parent

# A single connection is fine for this local, low-concurrency tool; SQLite
# in WAL mode serialises writes internally.
_conn: db.sqlite3.Connection | None = None


def get_conn() -> db.sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = db.connect()
        db.init_db(_conn)
    return _conn


def settings_dep() -> Settings:
    return get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Auto-load the bundled sample data on first run so the UI works
    # immediately with the documented one-line Docker command.
    conn = get_conn()
    if db.count_addresses(conn) == 0:
        sample = Path(get_settings().sample_csv)
        if sample.exists():
            ingest.add_many(conn, ingest.parse_csv(sample.read_bytes()))
    yield
    if _conn is not None:
        _conn.close()
        # Reset so a later startup (e.g. a second TestClient in tests)
        # opens a fresh connection instead of reusing the closed one.
        globals()["_conn"] = None


app = FastAPI(
    title="Address Matcher",
    description="把外部地址文本匹配到标准地址表（FastAPI + SQLite）",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _custom_openapi() -> dict[str, Any]:
    """Include the manually-validated experiment body in the Swagger schema."""
    from fastapi.openapi.utils import get_openapi

    if app.openapi_schema is not None:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version, description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in (ExperimentRequest, ExperimentCompareRequest):
        full = model.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        # Nested models (RuleProfile) land in $defs; hoist them next to the others.
        components.update(full.pop("$defs", {}))
        components[model.__name__] = full
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


# -------------------------------------------------------------------- pages --


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


# --------------------------------------------------------------------- api --


@app.get("/api/health")
def health(conn: db.sqlite3.Connection = Depends(get_conn),
           settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    return {
        "status": "ok",
        "address_count": db.count_addresses(conn),
        "needs_review": db.count_match_logs(conn, "needs_review"),
        "default_thresholds": {
            "auto_threshold": settings.auto_threshold,
            "margin_threshold": settings.margin_threshold,
            "reject_threshold": settings.reject_threshold,
            "token_weight": settings.token_weight,
        },
    }


# --- standard address table -------------------------------------------------


@app.get("/api/addresses")
def list_addresses(limit: int = 100, offset: int = 0,
                   conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    rows = db.list_addresses(conn, limit=limit, offset=offset)
    items = []
    for row in rows:
        items.append({
            "id": row["id"],
            "full_text": row["full_text"],
            "house": row["house"],
            "line1": row["line1"],
            "city": row["city"],
            "state": row["state"],
            "postal_code": row["postal_code"],
            "country": row["country"],
            "external_id": row["external_id"],
        })
    return {"total": db.count_addresses(conn), "items": items}


@app.post("/api/addresses")
def create_address(payload: AddressBatchIn,
                   conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = []
    for item in payload.addresses:
        raw = item.lookup_fields()
        raw["full_text"] = item.full_text or item.address
        rows.append(raw)
    with db.transaction(conn):
        summary = ingest.add_many(conn, rows, skip_duplicates=payload.skip_duplicates)
    return summary


@app.post("/api/addresses/upload")
async def upload_csv(file: UploadFile = File(...),
                     conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 .csv 文件")
    content = await file.read()
    try:
        rows = ingest.parse_csv(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"CSV 解析失败: {exc}") from exc
    summary = ingest.add_many(conn, rows)
    summary["filename"] = file.filename
    return summary


def _read_csv(file: UploadFile, content: bytes) -> None:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 .csv 文件")
    try:
        # Validate that the bytes really are decodable CSV up front.
        ingest.parse_csv_numbered(content)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"CSV 解析失败: {exc}") from exc


@app.post("/api/addresses/preview")
async def preview_csv(file: UploadFile = File(...),
                      conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Validate an uploaded CSV and return a confirmable preview voucher."""
    content = await file.read()
    _read_csv(file, content)
    return ingest.preview_csv(conn, content, file.filename)


@app.get("/api/addresses/preview/{token}/impact")
def preview_impact(token: str,
                   conn: db.sqlite3.Connection = Depends(get_conn),
                   settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    """导入影响预演：对预览凭证中的有效待导入行做内存干跑。

    服务端校验凭证未过期且地址表快照未变，用与正式导入相同的行规范化生成
    临时地址（以 CSV 行号标识），分别用当前地址表与临时合并表，沿现有匹配
    及复核共识逻辑重跑按日志时间倒序取出的最近 100 个不同原始查询；逐条
    对比前后状态与自动命中目标，分类为 改善 / 退化 / 目标改派 / 不变 并汇总。
    无效及重复行不参与；预演不写入地址和匹配日志，凭证保持有效。
    """
    try:
        return import_impact.compute_impact(conn, settings, token)
    except ingest.PreviewExpired as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ingest.PreviewTableChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/addresses/confirm")
async def confirm_csv(file: UploadFile = File(...),
                      preview_token: str = Form(...),
                      conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """Commit a preview: the file must be re-submitted together with its token.

    File digest and address-table snapshot bound to the voucher are re-checked
    inside the same SQLite write transaction that performs the inserts.
    """
    content = await file.read()
    _read_csv(file, content)
    try:
        return ingest.confirm_csv(conn, content, preview_token)
    except ingest.PreviewExpired as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ingest.PreviewError as exc:
        # File mismatch or table changed -> nothing is written, re-preview first.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/addresses/load-sample")
def load_sample(conn: db.sqlite3.Connection = Depends(get_conn),
                settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    sample = Path(settings.sample_csv)
    if not sample.exists():
        raise HTTPException(status_code=404, detail="未找到示例数据文件")
    rows = ingest.parse_csv(sample.read_bytes())
    summary = ingest.add_many(conn, rows)
    return summary


# --- matching ---------------------------------------------------------------


def _persist_match(conn: db.sqlite3.Connection, result: Any) -> int:
    best = result.best
    return db.insert_match_log(
        conn,
        query=result.query,
        status=result.status,
        score=best.score if best else None,
        candidate_id=best.address["id"] if best else None,
        token_score=best.token_score.f1 if best else None,
        house_score=best.house_score if best else None,
        matched_tokens=(
            best.token_score.street.matched + best.token_score.locality.matched
            if best else []
        ),
        conflicts=best.conflicts if best else [],
        candidates=[candidate_to_dict(c) for c in result.candidates],
        consensus=result.consensus,
    )


@app.post("/api/match")
def api_match(payload: MatchRequest,
              conn: db.sqlite3.Connection = Depends(get_conn),
              settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    if db.count_addresses(conn) == 0:
        raise HTTPException(status_code=400, detail="标准地址表为空，请先上传或加载示例数据")
    result = match_address(payload.query, db.all_addresses(conn), settings)
    # Scoring is unchanged; the reviewed-record consensus may redirect a
    # would-be needs_review result to an already-vouched standard address.
    verdict = consensus.lookup_consensus(conn, payload.query)
    consensus.apply_consensus(result, verdict)
    data = result_to_dict(result)
    if payload.save:
        with db.transaction(conn):
            data["match_id"] = _persist_match(conn, result)
    return data


@app.post("/api/match/batch")
def api_match_batch(payload: BatchMatchRequest,
                    conn: db.sqlite3.Connection = Depends(get_conn),
                    settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    if db.count_addresses(conn) == 0:
        raise HTTPException(status_code=400, detail="标准地址表为空，请先上传或加载示例数据")
    rows = db.all_addresses(conn)
    items = []
    with db.transaction(conn):
        for query in payload.queries:
            result = match_address(query, rows, settings)
            verdict = consensus.lookup_consensus(conn, query)
            consensus.apply_consensus(result, verdict)
            data = result_to_dict(result)
            if payload.save:
                data["match_id"] = _persist_match(conn, result)
            items.append(data)
    summary = {
        "total": len(items),
        "auto_matched": sum(1 for i in items if i["status"] == "auto_matched"),
        "needs_review": sum(1 for i in items if i["status"] == "needs_review"),
        "no_match": sum(1 for i in items if i["status"] == "no_match"),
    }
    return {"summary": summary, "results": items}


# --- review queue -----------------------------------------------------------


def _serialize_log(row: db.sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "query": row["query"],
        "status": row["status"],
        "review_action": row["review_action"],
        "score": row["score"],
        "candidate_id": row["candidate_id"],
        "selected_id": row["selected_id"],
        # Durable review-time snapshot (survives deletion of the address row).
        "selected_address_id": row["selected_address_id"],
        "selected_address_text": row["selected_address_text"],
        "token_score": row["token_score"],
        "house_score": row["house_score"],
        "matched_tokens": json.loads(row["matched_tokens"] or "[]"),
        "conflicts": json.loads(row["conflicts"] or "[]"),
        "candidates": json.loads(row["candidates_json"]),
        "consensus": json.loads(row["consensus_json"]) if row["consensus_json"] else None,
        "created_at": row["created_at"],
        "reviewed_at": row["reviewed_at"],
    }


@app.get("/api/matches")
def list_matches(status: str | None = None, limit: int = 100, offset: int = 0,
                 conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if status and status not in {"auto_matched", "needs_review", "no_match", "reviewed"}:
        raise HTTPException(status_code=400, detail="非法 status 过滤值")
    limit = max(1, min(limit, 500))
    rows = db.list_match_logs(conn, status=status, limit=limit, offset=offset)
    return {
        "total": db.count_match_logs(conn, status),
        "needs_review": db.count_match_logs(conn, "needs_review"),
        "items": [_serialize_log(r) for r in rows],
    }


@app.get("/api/matches/{match_id}")
def get_match(match_id: int,
              conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    row = db.get_match_log(conn, match_id)
    if row is None:
        raise HTTPException(status_code=404, detail="匹配记录不存在")
    return _serialize_log(row)


@app.post("/api/matches/{match_id}/review")
def review_match(match_id: int, payload: ReviewAction,
                 conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    row = db.get_match_log(conn, match_id)
    if row is None:
        raise HTTPException(status_code=404, detail="匹配记录不存在")

    score: float | None = None
    selected_text: str | None = None
    if payload.action == "overridden":
        if payload.selected_id is None:
            raise HTTPException(status_code=400, detail="改派时必须提供 selected_id")
        target = db.get_address(conn, payload.selected_id)
        if target is None:
            raise HTTPException(status_code=404, detail="所选标准地址不存在")
        selected_text = target["full_text"]
        candidates = json.loads(row["candidates_json"])
        for cand in candidates:
            if cand["address"]["id"] == payload.selected_id:
                score = cand["score"]
                break
    elif payload.action == "confirmed":
        payload = ReviewAction(action="confirmed", selected_id=row["candidate_id"])
        score = row["score"]
        # Persist the chosen address's own ID/full text even when it is only
        # referenced via candidate_id (the candidates_json is the snapshot).
        if row["candidate_id"] is not None:
            chosen = db.get_address(conn, row["candidate_id"])
            if chosen is not None:
                selected_text = chosen["full_text"]
            else:
                # Defensive: candidate row gone before review; restore the text
                # from the log's own candidate snapshot rather than losing it.
                for cand in json.loads(row["candidates_json"]):
                    if cand["address"]["id"] == row["candidate_id"]:
                        selected_text = cand["address"]["full_text"]
                        break

    with db.transaction(conn):
        db.review_match_log(
            conn,
            match_id,
            action=payload.action,
            selected_id=payload.selected_id,
            score=score,
            selected_text=selected_text,
        )
    return {"ok": True, "match_id": match_id, "action": payload.action,
            "selected_id": payload.selected_id}


# --- same-case merged review ------------------------------------------------


@app.get("/api/review-groups")
def list_review_groups(
    conn: db.sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """待确认记录按归一化道路/地区词元、门牌号归组（候选 ID/顺序/得分须一致）。"""
    groups = review_groups.compute_groups(conn)
    return {
        "needs_review": db.count_match_logs(conn, "needs_review"),
        "total_groups": len(groups),
        "groups": groups,
    }


@app.post("/api/review-groups/review")
def review_group(payload: GroupReviewAction,
                 conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """对整组执行确认首选 / 改派共同候选 / 判无匹配。

    服务端在同一个 SQLite 写事务内重新核对成员仍待确认、组签名与成员集合
    未变；若有变化返回 409 冲突且一条也不写入。
    """
    if payload.action == "overridden" and payload.selected_id is not None:
        if db.get_address(conn, payload.selected_id) is None:
            raise HTTPException(status_code=404, detail="所选标准地址不存在")
    try:
        result = review_groups.apply_group_review(
            conn,
            group_key=payload.group_key,
            match_ids=payload.match_ids,
            action=payload.action,
            selected_id=payload.selected_id,
        )
    except review_groups.GroupConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except review_groups.GroupReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}


# --- review verdict replay ---------------------------------------------------


@app.get("/api/review-replay")
def list_review_replay(category: str | None = None,
                       conn: db.sqlite3.Connection = Depends(get_conn),
                       settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    """复核结论回放：取全部已复核记录，以原查询在当前标准地址表上按默认规则
    重新匹配（纯评分，不查询复核共识记忆），逐条对照人工结论：

    确认/改派记录自动命中所选地址为 ``consistent``，命中其他地址为
    ``conflict``，未自动命中为 ``missed``；判无匹配记录出现自动命中为
    ``false_hit``，否则为 ``consistent``。人工所选地址的 ID 与全文取自
    复核写入时持久保存的快照（不随地址删除而消失），并用当前地址表判定
    所选地址是否已删除；历史记录若已失去可确定的所选 ID 则归为
    ``unknown``（原选择未知，不从候选列表猜测）。返回各类数量及原结论、
    当前首选与得分，便于定位规则或地址表变化带来的风险。

    只读操作：不写入匹配日志，也不改动任何复核记录。
    """
    if category is not None and category not in review_replay.VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail="非法 category 过滤值，仅支持 "
                   "consistent / conflict / missed / false_hit / unknown",
        )
    return review_replay.compute_replay(conn, settings, category=category)


# --- address coverage gaps --------------------------------------------------


@app.get("/api/coverage-gaps")
def list_coverage_gaps(status: str | None = None,
                       conn: db.sqlite3.Connection = Depends(get_conn),
                       settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    """地址覆盖缺口：从已保存的 ``no_match`` 日志按归一化道路/地区词元 +
    门牌号归组；每次调用都用当前标准地址表与默认规则重跑各组代表查询，
    自动命中的组标为 ``covered``（已覆盖），其余标为 ``open``（待补录）。

    只读操作：不写入、不改写任何匹配日志或评分。
    """
    if status is not None and status not in coverage_gaps.VALID_COVERAGE:
        raise HTTPException(
            status_code=400,
            detail="非法 status 过滤值，仅支持 open / covered",
        )
    return coverage_gaps.compute_gaps(conn, settings, coverage=status)


# --- standard-address self-match audit --------------------------------------


@app.get("/api/address-audit")
def list_address_audit(category: str | None = None,
                       conn: db.sqlite3.Connection = Depends(get_conn),
                       settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    """标准地址自匹配巡检：对当前表每条标准地址，以其全文作为查询、按默认规则
    在完整地址表上重新评分，只列出未自动命中自身 ID 的记录。

    每条风险给出自身分数，以及所有得分不低于「自身分数 − 默认分差阈值」的
    其他地址（不受前五候选截断），并按 自身分数不足 / 首选偏移 / 分差歧义
    分类汇总。只读操作：不写入任何匹配日志，也不查询复核共识记忆。
    """
    if category is not None and category not in address_audit.VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail="非法 category 过滤值，仅支持 "
                   "self_score_low / best_drifted / margin_ambiguous",
        )
    return address_audit.compute_audit(conn, settings, category=category)


# --- rule A/B comparison experiments ----------------------------------------


@app.post(
    "/api/experiments",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ExperimentRequest"}
                }
            },
        },
    },
)
def create_experiment(payload: dict[str, Any] = Body(...),
                      conn: db.sqlite3.Connection = Depends(get_conn),
                      settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    """提交规则 A/B 对照实验：2~4 个命名档案 + 1~100 条查询。

    档案参数非法或查询重复时逐项返回 ``errors`` 且不执行实验；
    通过校验后，所有档案在同一标准地址表快照上逐档运行现有匹配器。
    档案/查询数量、参数取值范围、档案重名、阈值次序与查询重复均逐项报错。
    """
    if db.count_addresses(conn) == 0:
        raise HTTPException(status_code=400, detail="标准地址表为空，请先上传或加载示例数据")
    try:
        return experiments.run_experiment(conn, payload, settings)
    except experiments.ExperimentValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": "请求参数校验失败", "errors": exc.errors},
        )


@app.get("/api/experiments")
def list_experiments(limit: int = 50, offset: int = 0,
                     conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    rows = db.list_ab_experiments(conn, limit=limit, offset=offset)
    return {
        "total": db.count_ab_experiments(conn),
        "items": [experiments.serialize_experiment_summary(r) for r in rows],
    }


@app.post(
    "/api/experiments/compare",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ExperimentCompareRequest"}
                }
            },
        },
    },
)
def compare_experiment(payload: Any = Body(...),
                       conn: db.sqlite3.Connection = Depends(get_conn)
                       ) -> dict[str, Any]:
    """历史回归对比：选择两次已持久化的实验逐档比较。

    服务端按**档案名称**与**去首尾空白后的查询**对齐两次实验；档案缺失
    （两次实验档案集合不一致）或任一实验存在重复查询时逐项报错并拒绝对比。
    对齐成功的每个「查询 × 档案」单元按状态 / 首选地址 / 得分变化分类为
    改善、退化、仅分数变化、无变化并汇总计数；地址表快照 digest 不同时
    标记为不可直接比较，但仍返回全部逐条差异。对比是纯推导，不写库。
    """
    try:
        return experiments.compare_experiments(conn, payload)
    except experiments.ExperimentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except experiments.ExperimentValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": "对比请求校验失败", "errors": exc.errors},
        )


@app.get("/api/experiments/{experiment_id}")
def get_experiment(experiment_id: str,
                   conn: db.sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    row = db.get_ab_experiment(conn, experiment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    return experiments.serialize_experiment(row)
