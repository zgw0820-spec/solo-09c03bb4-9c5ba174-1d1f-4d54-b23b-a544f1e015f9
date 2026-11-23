"""stitch 拼接核心单元测试：pytest 运行，或直接 python tests/test_stitch.py。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import stitch  # noqa: E402

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
CALLEE_BACKFILL = HEADER + "c2,callee,db1,999930,1000000\n"
CALLER_C1_V2 = HEADER + "c1,caller,web1,1000000,1001500\n"


def parse(text):
    return stitch.parse_probe_csv(text)


# ---------- 解析与整份拒收 ----------

def test_parse_basic_and_aliases():
    recs = parse(HEADER + "c1,caller,h1,10,20\nc2,服务方,h2,11,21\nc3,调用方,h3,12,22\n")
    assert [r.role for r in recs] == ["caller", "callee", "caller"]
    assert recs[0].host == "h1" and (recs[0].req_ts, recs[0].resp_ts) == (10, 20)


def test_parse_bom_and_blank_lines():
    recs = parse(chr(0xFEFF) + "\n\n" + CALLER_A)
    assert len(recs) == 3


def test_parse_dedup_inside_one_file():
    recs = parse(HEADER + "c1,caller,h1,10,20\nc1,caller,h1,10,20\n")
    assert len(recs) == 1


def test_parse_rejects_empty_and_missing_column():
    for bad in ("", "  \n", "call_id,role,host,req_ts\nc1,caller,h,1\n"):
        try:
            parse(bad)
        except stitch.ProbeFormatError:
            pass
        else:
            raise AssertionError("应当拒收：" + repr(bad))


def test_parse_rejects_bad_role():
    try:
        parse(HEADER + "c1,boss,h1,10,20\n")
    except stitch.ProbeFormatError as e:
        assert "role" in str(e)
    else:
        raise AssertionError("应当拒收非法 role")


def test_parse_rejects_non_integer_and_empty_fields():
    bad_ts = HEADER + "c1,caller,h1,十点,20\n"
    bad_host = HEADER + "c1,caller,,10,20\n"
    bad_id = HEADER + ",caller,h1,10,20\n"
    for bad in (bad_ts, bad_host, bad_id):
        try:
            parse(bad)
        except stitch.ProbeFormatError:
            pass
        else:
            raise AssertionError("应当拒收：" + bad)


def test_parse_rejects_extra_column():
    try:
        parse(HEADER + "c1,caller,h1,10,20,30\n")
    except stitch.ProbeFormatError as e:
        assert "列数" in str(e)
    else:
        raise AssertionError("应当拒收多列行")


# ---------- 批次：配对、候选、重选 ----------

def _board_with_pair():
    b = stitch.StitchBoard("b1")
    b.ingest(parse(CALLER_A), "caller_a.csv")
    b.ingest(parse(CALLEE_B), "callee_b.csv")
    return b


def test_ingest_pairs_and_marks_missing_side():
    b = _board_with_pair()
    snap = b.snapshot()
    by_id = {c["call_id"]: c for c in snap["calls"]}
    assert by_id["c1"]["status"] == "ready"
    assert by_id["c3"]["status"] == "ready"
    assert by_id["c2"]["status"] == "incomplete"
    assert by_id["c2"]["missing_roles"] == ["callee"]
    assert snap["stats"] == {"distinct_calls": 3, "complete_calls": 2, "hosts": 3, "files": 2}


def test_unique_candidate_auto_selected():
    b = _board_with_pair()
    cell = b._cells["c1"]["caller"]
    assert len(cell.candidates) == 1 and cell.selected_id == cell.candidates[0].candidate_id


def test_only_complete_calls_go_to_analysis():
    b = _board_with_pair()
    calls = b._complete_calls_locked()
    assert sorted(c.call_id for c in calls) == ["c1", "c3"]
    c1 = next(c for c in calls if c.call_id == "c1")
    # t1/t4 来自调用方，t2/t3 来自服务方
    assert (c1.t1, c1.t2, c1.t3, c1.t4) == (1000000, 1000620, 1000700, 1001350)
    assert (c1.caller, c1.callee) == ("web1", "app1")


def test_new_candidate_forces_reselection():
    b = _board_with_pair()
    old_sel = b._cells["c1"]["caller"].selected_id
    summary = b.ingest(parse(CALLER_C1_V2), "caller_a_v2.csv")
    assert summary["added_candidates"] == 1
    cell = b._cells["c1"]["caller"]
    assert len(cell.candidates) == 2
    assert cell.selected_id is None  # 新候选到达，旧选择作废
    assert old_sel in [c.candidate_id for c in cell.candidates]
    snap = b.snapshot()
    c1 = next(c for c in snap["calls"] if c["call_id"] == "c1")
    assert c1["needs_choice_roles"] == ["caller"]
    assert c1["status"] == "incomplete"


def test_manual_select_restores_completeness():
    b = _board_with_pair()
    b.ingest(parse(CALLER_C1_V2), "caller_a_v2.csv")
    v2 = next(
        c.candidate_id
        for c in b._cells["c1"]["caller"].candidates
        if c.record.resp_ts == 1001500
    )
    b.select("c1", "caller", v2)
    assert b._cells["c1"]["caller"].selected_id == v2
    assert sorted(c.call_id for c in b._complete_calls_locked()) == ["c1", "c3"]


def test_select_unknown_candidate_rejected():
    b = _board_with_pair()
    try:
        b.select("c1", "caller", "nope")
    except stitch.ProbeFormatError:
        pass
    else:
        raise AssertionError("不存在的候选应当拒收")


def test_resend_same_records_is_not_new_candidate():
    b = _board_with_pair()
    gen_before = b.generation
    sel_before = b._cells["c1"]["caller"].selected_id
    summary = b.ingest(parse(CALLER_A), "caller_a_retry.csv")
    assert summary["added_candidates"] == 0
    assert b.generation == gen_before  # 代次不变
    assert b._cells["c1"]["caller"].selected_id == sel_before
    # 来源文件仍然登记
    sources = b._cells["c1"]["caller"].candidates[0].sources
    assert sources == ["caller_a.csv", "caller_a_retry.csv"]


def test_backfill_without_changing_records_keeps_signature():
    b = _board_with_pair()
    # 先分析 c1+c3
    job1 = b.start_analysis(200)
    assert sorted(c.call_id for c in job1["calls"]) == ["c1", "c3"]
    b.publish_result(job1["job_id"], job1["generation"], job1["input_sig"], 200,
                     {"consistent": True}, None)
    assert b.analysis["state"] == "done"
    sig_before = b.input_signature_locked()

    # 原样重传：记录集合不变，签名不变，已发布结论保留
    b.ingest(parse(CALLER_A), "caller_a_retry.csv")
    assert b.input_signature_locked() == sig_before
    assert b.analysis["state"] == "done"

    # 补录缺失端 c2：输入集合变化，旧结论立即撤下
    b.ingest(parse(CALLEE_BACKFILL), "callee_backfill.csv")
    assert b.input_signature_locked() != sig_before
    assert b.analysis["state"] == "idle" and b.analysis["result"] is None
    assert sorted(c.call_id for c in b._complete_calls_locked()) == ["c1", "c2", "c3"]


def test_malformed_file_rejected_entirely():
    b = _board_with_pair()
    before = b.snapshot()
    bad = HEADER + "c9,caller,web1,十点,20\nc8,caller,web2,1,2\n"
    try:
        b.ingest(parse(bad), "bad.csv")
    except stitch.ProbeFormatError:
        pass
    else:
        raise AssertionError("格式错误的文件应整份拒收")
    after = b.snapshot()
    # 已收记录、文件清单、代次全都不变
    assert after["stats"] == before["stats"]
    assert after["files"] == before["files"]
    assert after["generation"] == before["generation"]
    assert {c["call_id"] for c in after["calls"]} == {"c1", "c2", "c3"}


def test_ingest_limit_over_capacity_rejected_entirely():
    b = stitch.StitchBoard()
    rows = "".join(
        f"k{i},caller,h{i % 16},{i},{i + 1}\n" for i in range(stitch.MAX_CALLS)
    )
    b.ingest(parse(HEADER + rows), "full.csv")
    before = b.snapshot()
    try:
        b.ingest(parse(HEADER + "extra,caller,hx,1,2\n"), "one_more.csv")
    except stitch.ProbeFormatError as e:
        assert str(stitch.MAX_CALLS) in str(e)
    else:
        raise AssertionError("超规模应整份拒收")
    assert b.snapshot()["files"] == before["files"]


# ---------- 分析代次：旧回包晚到 ----------

def test_stale_job_result_is_not_published():
    b = _board_with_pair()
    job1 = b.start_analysis(200)
    # 回包晚到之前，值班员重选/补录使代次推进、记录变化
    b.ingest(parse(CALLER_C1_V2), "caller_a_v2.csv")
    assert b.analysis["state"] == "idle"  # 旧结论立即撤下
    published = b.publish_result(
        job1["job_id"], job1["generation"], job1["input_sig"], 200,
        {"consistent": True, "stale": True}, None,
    )
    assert published is False
    assert b.analysis["state"] == "idle" and b.analysis["result"] is None

    # 新代次的回包可以正常发布
    v2 = next(
        c.candidate_id for c in b._cells["c1"]["caller"].candidates
        if c.record.resp_ts == 1001500
    )
    b.select("c1", "caller", v2)
    job2 = b.start_analysis(200)
    ok = b.publish_result(job2["job_id"], job2["generation"], job2["input_sig"], 200,
                          {"consistent": True, "fresh": True}, None)
    assert ok is True
    assert b.analysis["result"] == {"consistent": True, "fresh": True}


def test_same_signature_keeps_done_result():
    b = _board_with_pair()
    job = b.start_analysis(200)
    b.publish_result(job["job_id"], job["generation"], job["input_sig"], 200,
                     {"consistent": True}, None)
    # 原样重传不推进代次；再来一次同输入分析，旧 job 因 job_id 不匹配也不能覆盖
    b.ingest(parse(CALLER_A), "retry.csv")
    assert b.analysis["state"] == "done"
    job2 = b.start_analysis(200)
    stale = b.publish_result(job["job_id"], job["generation"], job["input_sig"], 200,
                             {"consistent": True, "old": True}, None)
    assert stale is False
    fresh = b.publish_result(job2["job_id"], job2["generation"], job2["input_sig"], 200,
                             {"consistent": True, "new": True}, None)
    assert fresh is True


def test_analyze_without_complete_call_rejected():
    b = stitch.StitchBoard()
    b.ingest(parse(HEADER + "c1,caller,h1,10,20\n"), "only_caller.csv")
    try:
        b.start_analysis(200)
    except stitch.ProbeFormatError:
        pass
    else:
        raise AssertionError("没有两端齐全的调用时不应允许分析")


def test_end_to_end_analysis_matches_solver():
    import solver
    b = _board_with_pair()
    b.ingest(parse(CALLEE_BACKFILL), "backfill.csv")
    job = b.start_analysis(200)
    calls = job["calls"]
    result = solver.analyze(calls, 200)
    assert b.publish_result(job["job_id"], job["generation"], job["input_sig"], 200, result, None)
    assert b.analysis["result"]["consistent"] is True
    # 与直接用 solver 解析等价四时刻 CSV 的结果一致
    direct_csv = (
        "call_id,caller,callee,req_send,req_recv,resp_send,resp_recv\n"
        "c1,web1,app1,1000000,1000620,1000700,1001350\n"
        "c2,app1,db1,1000650,999930,1000000,1001920\n"
        "c3,web1,db1,1000500,1000400,1000500,1002050\n"
    )
    direct = solver.analyze(solver.parse_csv(direct_csv), 200)
    assert direct["min_limit_us"] == result["min_limit_us"]


def test_concurrent_stale_job_timing(tmp_path=None):
    """模拟真实时序：旧分析耗时较久，期间记录变更，晚到结果必须被丢弃。"""
    b = _board_with_pair()
    job1 = b.start_analysis(200)
    time.sleep(0.05)
    b.ingest(parse(CALLER_C1_V2), "v2.csv")  # 记录变更，撤结论
    time.sleep(0.1)  # 旧回包晚到
    assert b.publish_result(
        job1["job_id"], job1["generation"], job1["input_sig"], 200, {"junk": 1}, None,
    ) is False
    assert b.analysis["result"] is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} 个测试全部通过")
