"""拼接页 API 端到端测试：pytest 运行。需要 fastapi（含 TestClient）。

通过环境变量让后台分析带人工延迟，以稳定复现“旧回包晚到”。
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("RPC_ANALYSIS_DELAY_MS", "300")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

client = TestClient(app)

HEADER = "call_id,role,host,req_ts,resp_ts\n"
CALLER_A = (
    HEADER
    + "c1,caller,web1,1000000,1001350\n"
    + "c2,caller,app1,1000650,1001920\n"
    + "c3,caller,web1,1000500,1002050\n"
)
CALLEE_B = (
    HEADER
    + "c1,callee,app1,1000620,1000700\n"
    + "c3,callee,db1,1000400,1000500\n"
)
BACKFILL_C2 = HEADER + "c2,callee,db1,999930,1000000\n"
CALLER_C1_V2 = HEADER + "c1,caller,web1,1000000,1001500\n"


def new_board():
    return client.post("/api/stitch/boards").json()["board"]["board_id"]


def upload(board_id, text, name="p.csv"):
    return client.post(
        f"/api/stitch/boards/{board_id}/upload",
        json={"filename": name, "csv_text": text},
    )


def wait_done(board_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = client.get(f"/api/stitch/boards/{board_id}").json()["board"]["analysis"]
        if a["state"] == "done":
            return a
        time.sleep(0.05)
    raise AssertionError("分析未在超时内完成")


def test_pages_served():
    assert client.get("/stitch").status_code == 200
    assert "RPC 双端日志拼接" in client.get("/stitch").text
    assert client.get("/").status_code == 200


def test_board_404():
    r = client.get("/api/stitch/boards/nope")
    assert r.status_code == 404 and r.json()["ok"] is False


def test_upload_pair_marks_missing_side():
    bid = new_board()
    r1 = upload(bid, CALLER_A, "caller_a.csv")
    assert r1.json()["ok"] and r1.json()["summary"]["added_candidates"] == 3
    r2 = upload(bid, CALLEE_B, "callee_b.csv")
    assert r2.json()["summary"]["added_candidates"] == 2
    board = r2.json()["board"]
    by_id = {c["call_id"]: c for c in board["calls"]}
    assert by_id["c1"]["status"] == "ready"
    assert by_id["c2"]["missing_roles"] == ["callee"]
    # 候选带来源文件
    c1_caller = by_id["c1"]["roles"]["caller"]["candidates"][0]
    assert c1_caller["sources"] == ["caller_a.csv"]


def test_malformed_file_whole_rejected():
    bid = new_board()
    upload(bid, CALLER_A, "caller_a.csv")
    before = client.get(f"/api/stitch/boards/{bid}").json()["board"]
    bad = HEADER + "c9,caller,web1,十点,20\n"
    r = upload(bid, bad, "bad.csv")
    assert r.status_code == 400 and "整数" in r.json()["error"]
    after = client.get(f"/api/stitch/boards/{bid}").json()["board"]
    assert after["stats"] == before["stats"]
    assert after["files"] == before["files"]
    assert after["generation"] == before["generation"]


def test_resend_is_not_new_candidate_and_keeps_conclusion():
    bid = new_board()
    upload(bid, CALLER_A, "caller_a.csv")
    upload(bid, CALLEE_B, "callee_b.csv")
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 200})
    assert r.json()["ok"]
    a1 = wait_done(bid)
    assert a1["result"]["consistent"] is True

    r = upload(bid, CALLER_A, "caller_a_retry.csv")
    summary = r.json()["summary"]
    assert summary["added_candidates"] == 0
    board = r.json()["board"]
    # 输入未变：原结论照常保留
    assert board["analysis"]["state"] == "done"
    assert board["analysis"]["generation"] == a1["generation"]
    src = board["calls"][0]["roles"]["caller"]["candidates"][0]["sources"]
    assert "caller_a_retry.csv" in src


def test_backfill_changes_input_and_republishes_fresh_result():
    bid = new_board()
    upload(bid, CALLER_A, "caller_a.csv")
    upload(bid, CALLEE_B, "callee_b.csv")
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 200})
    gen1 = r.json()["job"]["generation"]
    a1 = wait_done(bid)
    assert len(a1["result"]["calls"]) == 2  # 只有 c1、c3

    # 补录 c2 服务方：输入变化，旧结论立即撤下
    r = upload(bid, BACKFILL_C2, "backfill.csv")
    board = r.json()["board"]
    assert board["stats"]["complete_calls"] == 3
    assert board["analysis"]["state"] == "idle"
    assert board["analysis"]["result"] is None

    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 200})
    assert r.json()["job"]["generation"] > gen1
    a2 = wait_done(bid)
    assert len(a2["result"]["calls"]) == 3
    assert a2["result"]["consistent"] is True


def test_reselection_after_new_candidate():
    bid = new_board()
    upload(bid, CALLER_A, "caller_a.csv")
    upload(bid, CALLEE_B, "callee_b.csv")
    # 新候选到达：该端待重选，调用不再 ready
    r = upload(bid, CALLER_C1_V2, "caller_v2.csv")
    board = r.json()["board"]
    c1 = next(c for c in board["calls"] if c["call_id"] == "c1")
    assert c1["needs_choice_roles"] == ["caller"]
    assert c1["status"] == "incomplete"
    candidates = c1["roles"]["caller"]["candidates"]
    assert {c["resp_ts"] for c in candidates} == {1001350, 1001500}
    v2 = next(c["candidate_id"] for c in candidates if c["resp_ts"] == 1001500)

    # c1 待重选时不进入分析；c3 两端仍确定，可单独分析（只有确定的调用才送入）
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 200})
    assert r.status_code == 200
    pending = wait_done(bid)
    assert [c["call_id"] for c in pending["result"]["calls"]] == ["c3"]

    r = client.post(
        f"/api/stitch/boards/{bid}/select",
        json={"call_id": "c1", "role": "caller", "candidate_id": v2},
    )
    assert r.json()["ok"] and r.json()["summary"]["changed"] is True
    board = r.json()["board"]
    assert board["stats"]["complete_calls"] == 2
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 200})
    assert r.json()["ok"]
    a = wait_done(bid)
    c1_res = next(c for c in a["result"]["assignment"] if c["call_id"] == "c1")
    # 选定 v2（resp_ts=1001500）后，c1 的回程读数应反映新记录
    assert c1_res["call_id"] == "c1"


def test_stale_late_response_never_displayed():
    """旧回包晚到：分析期间上传新候选，旧任务完成后不得发布，页面只能拿到新代次结果。"""
    bid = new_board()
    upload(bid, CALLER_A, "caller_a.csv")
    upload(bid, CALLEE_B, "callee_b.csv")
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 200})
    gen_old = r.json()["job"]["generation"]
    assert r.json()["board"]["analysis"]["state"] == "running"

    # 分析进行中改变记录：立即撤下
    r = upload(bid, CALLER_C1_V2, "caller_v2.csv")
    assert r.json()["board"]["analysis"]["state"] == "idle"

    # 等旧任务回包（延迟 300ms），它必须被对版拦截
    time.sleep(0.6)
    board = client.get(f"/api/stitch/boards/{bid}").json()["board"]
    assert board["analysis"]["state"] == "idle"
    assert board["analysis"]["result"] is None
    assert board["analysis"]["generation"] is None

    # 重选后重新分析，新回包正常发布
    c1 = next(c for c in board["calls"] if c["call_id"] == "c1")
    v2 = next(
        c["candidate_id"] for c in c1["roles"]["caller"]["candidates"]
        if c["resp_ts"] == 1001500
    )
    client.post(
        f"/api/stitch/boards/{bid}/select",
        json={"call_id": "c1", "role": "caller", "candidate_id": v2},
    )
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 200})
    gen_new = r.json()["job"]["generation"]
    assert gen_new > gen_old
    a = wait_done(bid)
    assert a["generation"] == gen_new and a["result"]["consistent"] is True


def test_analyze_requires_complete_calls_and_valid_limit():
    bid = new_board()
    upload(bid, HEADER + "c1,caller,h1,10,20\n", "only.csv")
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 200})
    assert r.status_code == 400
    upload(bid, HEADER + "c1,callee,h2,11,15\n", "other.csv")
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": -1})
    assert r.status_code == 400
    r = client.post(f"/api/stitch/boards/{bid}/analyze", json={"limit_us": 0})
    assert r.status_code == 200
    wait_done(bid)
