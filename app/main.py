"""FastAPI 入口：RPC 双程耗时分析的网页与接口。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import solver

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "static" / "index.html"

app = FastAPI(title="RPC 双程耗时分析", version="1.0.0")


class AnalyzeRequest(BaseModel):
    limit_us: int
    csv_text: str


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
