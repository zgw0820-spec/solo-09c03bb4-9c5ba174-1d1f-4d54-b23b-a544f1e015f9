"""API 冒烟测试：pytest 运行。需要 fastapi（含 TestClient）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

client = TestClient(app)

SAMPLE_CSV = """\
call_id,caller,callee,req_send,req_recv,resp_send,resp_recv
c1,web1,app1,1000000,1000620,1000700,1000350
c2,app1,db1,1000650,999930,1000000,1000920
c3,web1,db1,1000500,1000400,1000500,1001050
"""


def test_index_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "RPC 双程耗时分析" in r.text


def test_health():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_analyze_feasible():
    r = client.post("/api/analyze", json={"limit_us": 250, "csv_text": SAMPLE_CSV})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["feasible_with_limit"] is True


def test_analyze_infeasible_min_limit():
    r = client.post("/api/analyze", json={"limit_us": 200, "csv_text": SAMPLE_CSV})
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["feasible_with_limit"] is False
    assert body["result"]["min_limit_us"] == 225


def test_analyze_bad_csv_400():
    r = client.post("/api/analyze", json={"limit_us": 100, "csv_text": "not,a,csv\n1,2,3\n"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_analyze_negative_limit_400():
    r = client.post("/api/analyze", json={"limit_us": -1, "csv_text": SAMPLE_CSV})
    assert r.status_code == 400
