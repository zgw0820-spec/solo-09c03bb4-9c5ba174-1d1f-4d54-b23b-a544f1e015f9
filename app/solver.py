"""RPC 双程耗时分析核心算法（纯标准库，无第三方依赖）。

问题模型
--------
每台主机 h 有一个本批内固定的未知钟差 o[h]（本地钟读数 = 真实时刻 + o[h]，整数微秒）。
一笔调用：调用方 a、服务方 b，四个时刻
  t1 请求发出（a 钟）、t2 请求到达（b 钟）、t3 应答发出（b 钟）、t4 应答到达（a 钟）。

去程耗时 L_out = (t2 - o[b]) - (t1 - o[a]) = (t2 - t1) - (o[b] - o[a])
回程耗时 L_ret = (t4 - o[a]) - (t3 - o[b]) = (t4 - t3) + (o[b] - o[a])

约束（全部是差分约束 o[v] - o[u] <= w）：
  自洽：L_out >= 0  =>  o[b] - o[a] <= t2 - t1
        L_ret >= 0  =>  o[a] - o[b] <= t4 - t3
  上限 L：L_out <= L  =>  o[a] - o[b] <= L - (t2 - t1)
          L_ret <= L  =>  o[b] - o[a] <= L - (t4 - t3)

算法：
  * 自洽性 / 上限可行性 / 求一组整数钟差：Bellman-Ford（差分约束经典解法，
    约束全为整数时解天然为整数）；
  * 矛盾定位：Bellman-Ford 前驱指针提取负环，给出可读的约束链；
  * 逐笔可达范围：Floyd-Warshall 全源最短路，o[b]-o[a] 的上下界即最短路；
  * 最小可行整数上限：可行性关于 L 单调，二分查找。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

MAX_CALLS = 80
MAX_HOSTS = 16

CSV_FIELDS = ("call_id", "caller", "callee", "req_send", "req_recv", "resp_send", "resp_recv")


@dataclass
class Call:
    call_id: str
    caller: str
    callee: str
    t1: int  # 请求发出（调用方时钟）
    t2: int  # 请求到达（服务方时钟）
    t3: int  # 应答发出（服务方时钟）
    t4: int  # 应答到达（调用方时钟）


def parse_csv(text: str) -> list[Call]:
    """解析并校验 CSV 文本，不合法时抛出 ValueError（中文错误信息）。"""
    if not text or not text.strip():
        raise ValueError("CSV 内容为空")
    text = text.lstrip("\ufeff")  # 去掉可能的 BOM
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)  # 容忍前导空行
    text = "\n".join(lines)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV 缺少表头")
    header = [h.strip() if h else "" for h in reader.fieldnames]
    missing = [f for f in CSV_FIELDS if f not in header]
    if missing:
        raise ValueError(
            "CSV 表头缺少列：{}；需要列：{}".format("、".join(missing), ",".join(CSV_FIELDS))
        )

    calls: list[Call] = []
    for row in reader:
        line = reader.line_num
        if None in row:
            raise ValueError(f"第 {line} 行：列数多于表头，请检查是否多了逗号")
        cleaned = {k.strip(): (v.strip() if v is not None else "") for k, v in row.items()}
        call_id = cleaned["call_id"]
        caller = cleaned["caller"]
        callee = cleaned["callee"]
        if not call_id:
            raise ValueError(f"第 {line} 行：call_id 为空")
        if not caller or not callee:
            raise ValueError(f"第 {line} 行：调用方/服务方主机名为空")
        try:
            t1, t2, t3, t4 = (
                int(cleaned["req_send"]),
                int(cleaned["req_recv"]),
                int(cleaned["resp_send"]),
                int(cleaned["resp_recv"]),
            )
        except ValueError:
            raise ValueError(f"第 {line} 行：四个时刻必须是整数微秒") from None
        calls.append(Call(call_id, caller, callee, t1, t2, t3, t4))

    if not calls:
        raise ValueError("CSV 中没有数据行")
    if len(calls) > MAX_CALLS:
        raise ValueError(f"每批最多 {MAX_CALLS} 笔调用，当前 {len(calls)} 笔")
    hosts = {c.caller for c in calls} | {c.callee for c in calls}
    if len(hosts) > MAX_HOSTS:
        raise ValueError(f"每批最多 {MAX_HOSTS} 台主机，当前 {len(hosts)} 台")
    return calls


def bellman_ford(n: int, edges: list[tuple]) -> list[int] | None:
    """差分约束求解：边 (u, v, w) 表示 x[v] - x[u] <= w。

    返回一组整数解（各分量 <= 0），存在负环（不可行）时返回 None。
    所有距离初始为 0，等价于从超级源点引 0 权边，非连通图同样适用。
    """
    dist = [0] * n
    for _ in range(n - 1):
        updated = False
        for u, v, w, *_ in edges:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                updated = True
        if not updated:
            break
    for u, v, w, *_ in edges:
        if dist[u] + w < dist[v]:
            return None
    return dist


def find_negative_cycle(n: int, edges: list[tuple]) -> tuple[list[tuple], int] | None:
    """定位负环：返回 (环上的边列表, 环总权值)，无负环返回 None。边可带第 4 元素作为说明。"""
    dist = [0] * n
    pred = [-1] * n  # pred[v] = 使 v 松弛的边的下标
    last = -1
    for _ in range(n):
        last = -1
        for k, edge in enumerate(edges):
            u, v, w = edge[0], edge[1], edge[2]
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                pred[v] = k
                last = v
    if last == -1:
        return None
    y = last
    for _ in range(n):
        y = edges[pred[y]][0]
    steps: list[tuple] = []
    seen: dict[int, int] = {}
    cur = y
    while cur not in seen:
        seen[cur] = len(steps)
        k = pred[cur]
        steps.append(edges[k])
        cur = edges[k][0]
    steps = steps[seen[cur]:]
    total = sum(e[2] for e in steps)
    return steps, total


def floyd_warshall(n: int, edges: list[tuple]) -> list[list[float]]:
    """全源最短路（调用前需保证无负环）。d[i][j] 即 x[j] - x[i] 的最紧上界。"""
    inf = float("inf")
    d = [[inf] * n for _ in range(n)]
    for i in range(n):
        d[i][i] = 0
    for u, v, w, *_ in edges:
        if w < d[u][v]:
            d[u][v] = w
    for k in range(n):
        dk = d[k]
        for i in range(n):
            di = d[i]
            ik = di[k]
            if ik == inf:
                continue
            for j in range(n):
                kj = dk[j]
                if kj != inf and ik + kj < di[j]:
                    di[j] = ik + kj
    return d


def _latencies(calls: list[Call], idx: dict[str, int], sol: list[int]) -> list[dict]:
    """按给定钟差计算每笔调用的去程/回程耗时。"""
    out = []
    for c in calls:
        oa = sol[idx[c.caller]]
        ob = sol[idx[c.callee]]
        out.append(
            {
                "call_id": c.call_id,
                "caller": c.caller,
                "callee": c.callee,
                "out_us": (c.t2 - c.t1) - (ob - oa),
                "ret_us": (c.t4 - c.t3) + (ob - oa),
            }
        )
    return out


def analyze(calls: list[Call], limit_us: int) -> dict:
    """主分析入口，返回可 JSON 序列化的结果字典。"""
    hosts = sorted({c.caller for c in calls} | {c.callee for c in calls})
    idx = {h: i for i, h in enumerate(hosts)}
    n = len(hosts)

    # 自洽约束边 (u, v, w, 说明)：o[v] - o[u] <= w
    base_edges: list[tuple] = []
    for c in calls:
        a, b = idx[c.caller], idx[c.callee]
        base_edges.append(
            (a, b, c.t2 - c.t1, f"o[{c.callee}] − o[{c.caller}] ≤ {c.t2 - c.t1}（调用 {c.call_id} 去程非负）")
        )
        base_edges.append(
            (b, a, c.t4 - c.t3, f"o[{c.caller}] − o[{c.callee}] ≤ {c.t4 - c.t3}（调用 {c.call_id} 回程非负）")
        )

    # 1) 自洽性
    cycle = find_negative_cycle(n, base_edges)
    if cycle is not None:
        steps, total = cycle
        return {
            "consistent": False,
            "hosts": hosts,
            "limit_us": limit_us,
            "contradiction": {
                "total_us": total,
                "steps": [e[3] for e in steps],
            },
        }

    # 2) 逐笔去程/回程可达范围（仅基于自洽约束）
    dist = floyd_warshall(n, base_edges)
    call_ranges = []
    for c in calls:
        a, b = idx[c.caller], idx[c.callee]
        d_ab = dist[a][b]  # o[b] - o[a] 的最大值
        d_ba = dist[b][a]  # o[a] - o[b] 的最大值
        out_min = (c.t2 - c.t1) - d_ab
        out_max = (c.t2 - c.t1) + d_ba
        ret_min = (c.t4 - c.t3) - d_ba
        ret_max = (c.t4 - c.t3) + d_ab
        call_ranges.append(
            {
                "call_id": c.call_id,
                "caller": c.caller,
                "callee": c.callee,
                "out_min": out_min,
                "out_max": out_max,
                "ret_min": ret_min,
                "ret_max": ret_max,
                "must_exceed": out_min > limit_us or ret_min > limit_us,
            }
        )

    # 3) 叠加上限约束后的可行性
    def edges_with_limit(limit: int) -> list[tuple]:
        edges = [(u, v, w) for u, v, w, _ in base_edges]
        for c in calls:
            a, b = idx[c.caller], idx[c.callee]
            edges.append((b, a, limit - (c.t2 - c.t1)))  # 去程 <= limit
            edges.append((a, b, limit - (c.t4 - c.t3)))  # 回程 <= limit
        return edges

    def offsets_dict(sol: list[int]) -> dict[str, int]:
        base = min(sol)  # 整体平移不影响耗时，归零便于阅读
        return {h: sol[i] - base for i, h in enumerate(hosts)}

    result: dict = {
        "consistent": True,
        "hosts": hosts,
        "limit_us": limit_us,
        "calls": call_ranges,
    }

    sol = bellman_ford(n, edges_with_limit(limit_us))
    if sol is not None:
        result["feasible_with_limit"] = True
        result["min_limit_us"] = None
        result["offsets"] = offsets_dict(sol)
        result["assignment"] = _latencies(calls, idx, sol)
        result["assignment_note"] = "满足当前上限的一组钟差"
        return result

    # 4) 自洽但无法同时达标：二分最小可行整数上限
    lo, hi = -1, 1  # lo 不可行（至少一笔调用时 -1 必不可行），倍增找可行的 hi
    while bellman_ford(n, edges_with_limit(hi)) is None:
        hi *= 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if bellman_ford(n, edges_with_limit(mid)) is not None:
            hi = mid
        else:
            lo = mid
    sol = bellman_ford(n, edges_with_limit(hi))
    result["feasible_with_limit"] = False
    result["min_limit_us"] = hi
    result["offsets"] = offsets_dict(sol)
    result["assignment"] = _latencies(calls, idx, sol)
    result["assignment_note"] = "满足最小可行上限的一组钟差"
    return result
