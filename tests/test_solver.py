"""solver 单元测试：pytest 运行，或直接 python tests/test_solver.py。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import solver  # noqa: E402

SAMPLE_CSV = """\
call_id,caller,callee,req_send,req_recv,resp_send,resp_recv
c1,web1,app1,1000000,1000620,1000700,1000350
c2,app1,db1,1000650,999930,1000000,1000920
c3,web1,db1,1000500,1000400,1000500,1001050
"""

CONTRADICTION_CSV = """\
call_id,caller,callee,req_send,req_recv,resp_send,resp_recv
x1,web1,app1,1000000,1000100,1000400,1000200
"""


def check_solution(calls, offsets, limit):
    """独立验证：给定钟差，逐笔重算两程耗时，须落在 [0, limit]。"""
    for c in calls:
        d = offsets[c.callee] - offsets[c.caller]
        out = (c.t2 - c.t1) - d
        ret = (c.t4 - c.t3) + d
        assert 0 <= out <= limit, (c.call_id, "out", out, limit)
        assert 0 <= ret <= limit, (c.call_id, "ret", ret, limit)


def test_parse_ok():
    calls = solver.parse_csv(SAMPLE_CSV)
    assert len(calls) == 3
    assert calls[0].call_id == "c1"
    assert (calls[1].t1, calls[1].t2, calls[1].t3, calls[1].t4) == (1000650, 999930, 1000000, 1000920)


def test_parse_ok_with_bom_and_blank_lines():
    calls = solver.parse_csv("\ufeff\n\n" + SAMPLE_CSV.replace("c2", "\nc2", 1))
    assert len(calls) == 3


def test_parse_rejects_empty():
    for bad in ("", "   \n", "call_id,caller,callee,req_send,req_recv,resp_send,resp_recv\n"):
        try:
            solver.parse_csv(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("应当拒绝空 CSV")


def test_parse_rejects_missing_column():
    bad = "call_id,caller,callee,req_send,req_recv,resp_send\nc1,a,b,1,2,3\n"
    try:
        solver.parse_csv(bad)
    except ValueError as e:
        assert "resp_recv" in str(e)
    else:
        raise AssertionError("应当拒绝缺列")


def test_parse_rejects_non_integer_timestamp():
    bad = SAMPLE_CSV.replace("1000620", "10.5")
    try:
        solver.parse_csv(bad)
    except ValueError as e:
        assert "整数" in str(e)
    else:
        raise AssertionError("应当拒绝非整数时刻")


def test_parse_rejects_too_many_calls():
    header = "call_id,caller,callee,req_send,req_recv,resp_send,resp_recv\n"
    rows = "".join(f"k{i},a,b,{i},{i + 1},{i + 2},{i + 3}\n" for i in range(81))
    try:
        solver.parse_csv(header + rows)
    except ValueError as e:
        assert "80" in str(e)
    else:
        raise AssertionError("应当拒绝超过 80 笔")


def test_parse_rejects_too_many_hosts():
    header = "call_id,caller,callee,req_send,req_recv,resp_send,resp_recv\n"
    rows = "".join(f"k{i},h{i},hx,{i},{i + 1},{i + 2},{i + 3}\n" for i in range(16))
    try:
        solver.parse_csv(header + rows)
    except ValueError as e:
        assert "16" in str(e)
    else:
        raise AssertionError("应当拒绝超过 16 台主机")


def test_ranges_of_sample():
    calls = solver.parse_csv(SAMPLE_CSV)
    r = solver.analyze(calls, 200)
    assert r["consistent"] is True
    got = {c["call_id"]: c for c in r["calls"]}
    assert (got["c1"]["out_min"], got["c1"]["out_max"]) == (0, 270)
    assert (got["c1"]["ret_min"], got["c1"]["ret_max"]) == (0, 270)
    assert (got["c2"]["out_min"], got["c2"]["out_max"]) == (0, 200)
    assert (got["c2"]["ret_min"], got["c2"]["ret_max"]) == (0, 200)
    assert (got["c3"]["out_min"], got["c3"]["out_max"]) == (0, 450)
    assert (got["c3"]["ret_min"], got["c3"]["ret_max"]) == (0, 450)


def test_feasible_with_limit_250():
    calls = solver.parse_csv(SAMPLE_CSV)
    r = solver.analyze(calls, 250)
    assert r["consistent"] and r["feasible_with_limit"] is True
    assert r["min_limit_us"] is None
    check_solution(calls, r["offsets"], 250)
    # 页面展示的逐笔耗时与钟差一致
    for a in r["assignment"]:
        assert 0 <= a["out_us"] <= 250 and 0 <= a["ret_us"] <= 250


def test_infeasible_200_min_limit_225():
    calls = solver.parse_csv(SAMPLE_CSV)
    r = solver.analyze(calls, 200)
    assert r["consistent"] and r["feasible_with_limit"] is False
    assert r["min_limit_us"] == 225
    check_solution(calls, r["offsets"], 225)
    # 224 仍不可行，225 可行：确认是最小整数上限
    assert solver.analyze(calls, 224)["feasible_with_limit"] is False
    assert solver.analyze(calls, 225)["feasible_with_limit"] is True


def test_contradiction_prompts_remeasure():
    calls = solver.parse_csv(CONTRADICTION_CSV)
    r = solver.analyze(calls, 1000)
    assert r["consistent"] is False
    assert r["contradiction"]["total_us"] == -100
    assert any("x1" in s for s in r["contradiction"]["steps"])


def test_self_call():
    csv_text = (
        "call_id,caller,callee,req_send,req_recv,resp_send,resp_recv\n"
        "s1,h1,h1,100,150,200,260\n"
    )
    calls = solver.parse_csv(csv_text)
    r = solver.analyze(calls, 100)
    assert r["consistent"] and r["feasible_with_limit"] is True
    c = r["calls"][0]
    assert (c["out_min"], c["out_max"]) == (50, 50)  # 同主机钟差为 0，范围退化为点
    assert (c["ret_min"], c["ret_max"]) == (60, 60)


def test_disconnected_components():
    csv_text = (
        "call_id,caller,callee,req_send,req_recv,resp_send,resp_recv\n"
        "a1,h1,h2,1000,1050,1100,1180\n"
        "b1,h3,h4,2000,2070,2100,2190\n"
    )
    calls = solver.parse_csv(csv_text)
    r = solver.analyze(calls, 100)
    assert r["consistent"] and r["feasible_with_limit"] is True
    check_solution(calls, r["offsets"], 100)
    got = {c["call_id"]: c for c in r["calls"]}
    assert (got["a1"]["out_min"], got["a1"]["out_max"]) == (0, 130)
    assert (got["b1"]["ret_min"], got["b1"]["ret_max"]) == (0, 160)


def test_big_epoch_timestamps():
    shift = 1_700_000_000_000_000  # 微秒级 Unix 时间戳
    lines = SAMPLE_CSV.strip().splitlines()
    out_lines = [lines[0]]
    for line in lines[1:]:
        parts = line.split(",")
        parts[3:] = [str(int(v) + shift) for v in parts[3:]]
        out_lines.append(",".join(parts))
    calls = solver.parse_csv("\n".join(out_lines) + "\n")
    r = solver.analyze(calls, 200)
    assert r["consistent"] and r["feasible_with_limit"] is False
    assert r["min_limit_us"] == 225
    check_solution(calls, r["offsets"], 225)


def test_zero_limit_possible():
    csv_text = (
        "call_id,caller,callee,req_send,req_recv,resp_send,resp_recv\n"
        "z1,a,b,100,100,100,100\n"
    )
    calls = solver.parse_csv(csv_text)
    r = solver.analyze(calls, 0)
    assert r["consistent"] and r["feasible_with_limit"] is True
    check_solution(calls, r["offsets"], 0)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} 个测试全部通过")
