"""FastAPI 入口：RPC 双程耗时分析（单程分析 + 双端日志拼接页）。"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import solver
import stitch

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "static" / "index.html"
STITCH_HTML = BASE_DIR / "static" / "stitch.html"

# 演示“回包有快有慢”：分析在后台线程执行，可通过环境变量注入人工延迟（毫秒）。
# 在提交任务时读取，便于测试通过环境变量稳定复现“旧回包晚到”。
def _analysis_delay_ms() -> int:
    return int(os.environ.get("RPC_ANALYSIS_DELAY_MS", "0"))

app = FastAPI(title="RPC 双程耗时分析", version="2.0.0")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rpc-analysis")
_boards: dict[str, stitch.StitchBoard] = {}


class AnalyzeRequest(BaseModel):
    limit_us: int
    csv_text: str


class UploadRequest(BaseModel):
    filename: str
    csv_text: str


class SelectRequest(BaseModel):
    call_id: str
    role: str
    candidate_id: str


class StitchAnalyzeRequest(BaseModel):
    limit_us: int


def _get_board(board_id: str) -> stitch.StitchBoard:
    board = _boards.get(board_id)
    if board is None:
        raise KeyError(board_id)
    return board


def _bad_request(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


def _run_analysis_job(
    board: stitch.StitchBoard,
    job_id: str,
    generation: int,
    input_sig: tuple,
    limit_us: int,
    calls: list,
    delay_ms: int,
) -> None:
    """后台线程：执行现有单程分析，完成后凭代次/签名对版再发布。"""
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    try:
        result = solver.analyze(calls, limit_us)
        board.publish_result(job_id, generation, input_sig, limit_us, result, None)
    except Exception as exc:  # 分析本身异常同样要对版，且不能冲掉新结论
        board.publish_result(job_id, generation, input_sig, limit_us, None, str(exc))


# ---------- 原有单程分析 ----------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_HTML)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/analyze")
def analyze_api(req: AnalyzeRequest):
    if req.limit_us < 0:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "单程耗时上限必须是非负整数（微秒）"},
        )
    try:
        calls = solver.parse_csv(req.csv_text)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    return {"ok": True, "result": solver.analyze(calls, req.limit_us)}


# ---------- 双端日志拼接 ----------

@app.get("/stitch", include_in_schema=False)
def stitch_page():
    return FileResponse(STITCH_HTML)


@app.post("/api/stitch/boards")
def create_board():
    board = stitch.StitchBoard()
    _boards[board.board_id] = board
    return {"ok": True, "board": board.snapshot()}


@app.get("/api/stitch/boards/{board_id}")
def get_board(board_id: str):
    try:
        board = _get_board(board_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"ok": False, "error": "批次不存在或已过期"})
    return {"ok": True, "board": board.snapshot()}


@app.post("/api/stitch/boards/{board_id}/upload")
def upload_probe(board_id: str, req: UploadRequest):
    try:
        board = _get_board(board_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"ok": False, "error": "批次不存在或已过期"})
    filename = (req.filename or "").strip() or "未命名.csv"
    try:
        # 先整份解析：抛错则 board 状态完全不被触碰（整份拒收）
        records = stitch.parse_probe_csv(req.csv_text)
        summary = board.ingest(records, filename)
    except stitch.ProbeFormatError as exc:
        return _bad_request(exc)
    return {"ok": True, "summary": summary, "board": board.snapshot()}


@app.post("/api/stitch/boards/{board_id}/select")
def select_candidate(board_id: str, req: SelectRequest):
    try:
        board = _get_board(board_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"ok": False, "error": "批次不存在或已过期"})
    try:
        summary = board.select(req.call_id, req.role, req.candidate_id)
    except stitch.ProbeFormatError as exc:
        return _bad_request(exc)
    return {"ok": True, "summary": summary, "board": board.snapshot()}


@app.post("/api/stitch/boards/{board_id}/analyze")
def start_stitch_analysis(board_id: str, req: StitchAnalyzeRequest):
    try:
        board = _get_board(board_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"ok": False, "error": "批次不存在或已过期"})
    if req.limit_us < 0:
        return _bad_request(stitch.ProbeFormatError("单程耗时上限必须是非负整数（微秒）"))
    try:
        job = board.start_analysis(req.limit_us)
    except stitch.ProbeFormatError as exc:
        return _bad_request(exc)
    _executor.submit(
        _run_analysis_job,
        board,
        job["job_id"],
        job["generation"],
        job["input_sig"],
        req.limit_us,
        job["calls"],
        _analysis_delay_ms(),
    )
    return {
        "ok": True,
        "job": {"job_id": job["job_id"], "generation": job["generation"]},
        "board": board.snapshot(),
    }
