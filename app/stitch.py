"""RPC 双端日志拼接（纯标准库）。

值班员把调用方 / 服务方探针分次导出的同一短窗口 CSV 传到同一批次（board）：
每行只含本端记录 —— 调用号、本端角色、主机名、本端记录的请求/应答时刻。

本模块负责：
  * 探针 CSV 的整份解析与校验（任何一行不合法即整份拒收，不碰已收记录）；
  * 按 (调用号, 角色) 收集候选记录并去重（原样重传不算新候选）；
  * 同一端出现不同记录时列出候选及来源文件，由值班员选定；来了新候选需重选；
  * 两端都确定的调用拼成 solver.Call 交给现有单程分析；
  * 维护“用于分析的记录签名”，记录一变即让旧结论作废（见 StitchBoard）。

并发模型：一次分析在后台线程运行（模拟探针回包有快有慢），StitchBoard 内部
用一把锁保护全部可变状态；分析完成后凭 generation/input_sig 对版，旧代次或
签名对不上的结果一律不得发布。
"""

from __future__ import annotations

import csv
import io
import threading
import uuid
from dataclasses import dataclass, field

import solver

# 与单程分析保持同一规模上限
MAX_CALLS = solver.MAX_CALLS
MAX_HOSTS = solver.MAX_HOSTS
MAX_FILES = 40

PROBE_FIELDS = ("call_id", "role", "host", "req_ts", "resp_ts")
ROLE_CALLER = "caller"
ROLE_CALLEE = "callee"
ROLE_ALIASES = {
    "caller": ROLE_CALLER,
    "callee": ROLE_CALLEE,
    "调用方": ROLE_CALLER,
    "服务方": ROLE_CALLEE,
}


@dataclass(frozen=True)
class ProbeRecord:
    """一条探针记录（某个调用号在某一端的本端观测）。"""

    call_id: str
    role: str  # caller | callee
    host: str
    req_ts: int
    resp_ts: int

    def as_tuple(self) -> tuple:
        return (self.call_id, self.role, self.host, self.req_ts, self.resp_ts)


@dataclass
class Candidate:
    """同一 (调用号, 角色) 下的一条候选记录；sources 为来源文件名（去重保序）。"""

    candidate_id: str
    record: ProbeRecord
    sources: list[str] = field(default_factory=list)


@dataclass
class Cell:
    """一个 (调用号, 角色) 格子：候选列表 + 当前选定的 candidate_id（未选为 None）。"""

    candidates: list[Candidate] = field(default_factory=list)
    selected_id: str | None = None


class ProbeFormatError(ValueError):
    """探针 CSV 格式错误：整份拒收。"""


def parse_probe_csv(text: str) -> list[ProbeRecord]:
    """解析探针 CSV 文本，任何一行不合法都抛 ProbeFormatError（中文信息含行号）。

    成功才返回记录列表；调用方应在解析成功后再动批次状态，保证“整份拒收”。
    """
    if not text or not text.strip():
        raise ProbeFormatError("CSV 内容为空")
    text = text.lstrip(chr(0xFEFF))  # 容忍 BOM
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)  # 容忍前导空行
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if reader.fieldnames is None:
        raise ProbeFormatError("CSV 缺少表头")
    header = [h.strip() if h else "" for h in reader.fieldnames]
    missing = [f for f in PROBE_FIELDS if f not in header]
    if missing:
        raise ProbeFormatError(
            "CSV 表头缺少列：{}；需要列：{}".format("、".join(missing), ",".join(PROBE_FIELDS))
        )

    records: list[ProbeRecord] = []
    seen: set[tuple] = set()
    for row in reader:
        line = reader.line_num
        if None in row:
            raise ProbeFormatError(f"第 {line} 行：列数多于表头，请检查是否多了逗号")
        cleaned = {k.strip(): (v.strip() if v is not None else "") for k, v in row.items()}
        call_id = cleaned["call_id"]
        host = cleaned["host"]
        role_raw = cleaned["role"].lower()
        if not call_id:
            raise ProbeFormatError(f"第 {line} 行：call_id 为空")
        if role_raw not in ROLE_ALIASES:
            raise ProbeFormatError(
                f"第 {line} 行：role 只能是 caller（调用方）或 callee（服务方），当前为 {cleaned['role']!r}"
            )
        if not host:
            raise ProbeFormatError(f"第 {line} 行：host 主机名为空")
        try:
            req_ts = int(cleaned["req_ts"])
            resp_ts = int(cleaned["resp_ts"])
        except ValueError:
            raise ProbeFormatError(f"第 {line} 行：请求/应答时刻必须是整数微秒") from None
        rec = ProbeRecord(call_id, ROLE_ALIASES[role_raw], host, req_ts, resp_ts)
        if rec.as_tuple() not in seen:  # 同一文件内重复行视为一条
            seen.add(rec.as_tuple())
            records.append(rec)

    if not records:
        raise ProbeFormatError("CSV 中没有数据行")
    return records


def _build_call(call_id: str, caller_rec: ProbeRecord, callee_rec: ProbeRecord) -> solver.Call:
    """把两端记录拼成单程分析所需的四时刻调用。

    调用方记录：req_ts=t1（请求发出）、resp_ts=t4（应答到达）；
    服务方记录：req_ts=t2（请求到达）、resp_ts=t3（应答发出）。
    """
    return solver.Call(
        call_id=call_id,
        caller=caller_rec.host,
        callee=callee_rec.host,
        t1=caller_rec.req_ts,
        t2=callee_rec.req_ts,
        t3=callee_rec.resp_ts,
        t4=caller_rec.resp_ts,
    )


class StitchBoard:
    """一个拼接批次的全部状态。所有方法自带锁，可被 Web 工作线程直接调用。"""

    def __init__(self, board_id: str | None = None):
        self.board_id = board_id or uuid.uuid4().hex[:12]
        self._lock = threading.RLock()
        # cells[call_id][role] -> Cell
        self._cells: dict[str, dict[str, Cell]] = {}
        # 已接收文件（用于页面展示与计数）
        self.files: list[dict] = []
        self._seq = 0
        # 分析状态
        self.generation = 0          # 任何可能改变分析输入的动作都 +1
        self.analysis = {            # 最近一次“已发布”的结论
            "state": "idle",         # idle | running | done
            "generation": None,
            "input_sig": None,
            "limit_us": None,
            "result": None,
            "error": None,
            "job_id": None,
        }

    # ---------- 内部工具 ----------

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def _cell(self, call_id: str, role: str) -> Cell:
        return self._cells.setdefault(call_id, {}).setdefault(role, Cell())

    def _all_hosts_locked(self) -> set[str]:
        hosts: set[str] = set()
        for roles in self._cells.values():
            for cell in roles.values():
                for cand in cell.candidates:
                    hosts.add(cand.record.host)
        return hosts

    def _complete_calls_locked(self) -> list[solver.Call]:
        """两端都已选定的调用，按调用号排序拼成 solver.Call。"""
        out: list[solver.Call] = []
        for call_id in sorted(self._cells.keys()):
            roles = self._cells[call_id]
            caller_cell = roles.get(ROLE_CALLER)
            callee_cell = roles.get(ROLE_CALLEE)
            if not caller_cell or not callee_cell:
                continue
            if caller_cell.selected_id is None or callee_cell.selected_id is None:
                continue
            caller_rec = self._selected_record_locked(caller_cell)
            callee_rec = self._selected_record_locked(callee_cell)
            out.append(_build_call(call_id, caller_rec, callee_rec))
        return out

    @staticmethod
    def _selected_record_locked(cell: Cell) -> ProbeRecord:
        return next(c.record for c in cell.candidates if c.candidate_id == cell.selected_id)

    def input_signature_locked(self) -> tuple | None:
        """当前分析输入（两端确定的调用）的内容签名；为空返回 None。

        签名只取决于记录内容，与来源文件名无关：因此原样重传 / 不改变本批记录
        的补录不会改变签名，旧结论照常保留。
        """
        calls = self._complete_calls_locked()
        if not calls:
            return None
        return tuple(
            (c.call_id, c.caller, c.callee, c.t1, c.t2, c.t3, c.t4) for c in calls
        )

    def _bump_generation_locked(self) -> int:
        self.generation += 1
        # 用于分析的记录可能已变：立即撤下旧结论（running 标记保留，结果由对版拦截）
        self.analysis["state"] = "idle"
        self.analysis["result"] = None
        self.analysis["error"] = None
        self.analysis["input_sig"] = None
        self.analysis["limit_us"] = None
        self.analysis["job_id"] = None
        self.analysis["generation"] = None
        return self.generation

    # ---------- 上传 / 选择 ----------

    def ingest(self, records: list[ProbeRecord], filename: str) -> dict:
        """把一份已通过校验的探针 CSV 收入批次。返回变更摘要。"""
        with self._lock:
            if len(self.files) >= MAX_FILES:
                raise ProbeFormatError(f"每个批次最多接收 {MAX_FILES} 份文件")

            # 先在影子结构上做规模校验，超限则整份拒收、不动已收记录
            shadow_calls = set(self._cells.keys())
            shadow_hosts = self._all_hosts_locked()
            for rec in records:
                shadow_calls.add(rec.call_id)
                shadow_hosts.add(rec.host)
            if len(shadow_calls) > MAX_CALLS:
                raise ProbeFormatError(
                    f"接收后调用号将达 {len(shadow_calls)} 个，超过每批最多 {MAX_CALLS} 笔，整份拒收"
                )
            if len(shadow_hosts) > MAX_HOSTS:
                raise ProbeFormatError(
                    f"接收后主机将达 {len(shadow_hosts)} 台，超过每批最多 {MAX_HOSTS} 台，整份拒收"
                )

            added_candidates = 0
            touched_cells: list[tuple[str, str]] = []
            for rec in records:
                cell = self._cell(rec.call_id, rec.role)
                match = next(
                    (c for c in cell.candidates if c.record.as_tuple() == rec.as_tuple()), None
                )
                if match is None:
                    # 新候选到达：无论此前是否选定过，该端都必须重选
                    cand = Candidate(self._next_id("cand"), rec, [filename])
                    was_empty = not cell.candidates
                    cell.candidates.append(cand)
                    cell.selected_id = cand.candidate_id if was_empty else None
                    added_candidates += 1
                    touched_cells.append((rec.call_id, rec.role))
                else:
                    if filename not in match.sources:
                        match.sources.append(filename)

            self.files.append(
                {
                    "name": filename,
                    "rows": len(records),
                    "new_candidates": added_candidates,
                }
            )

            if added_candidates:
                gen = self._bump_generation_locked()
            else:
                gen = self.generation  # 原样重传：不改变记录，结论照常保留
            return {
                "added_candidates": added_candidates,
                "touched_cells": touched_cells,
                "generation": gen,
                "records": len(records),
                "filename": filename,
            }

    def select(self, call_id: str, role: str, candidate_id: str) -> dict:
        """值班员为某一端选定候选。选择改变了分析输入时旧结论立即撤下。"""
        with self._lock:
            if role not in (ROLE_CALLER, ROLE_CALLEE):
                raise ProbeFormatError("role 只能是 caller 或 callee")
            roles = self._cells.get(call_id)
            if not roles or role not in roles:
                raise ProbeFormatError(f"调用号 {call_id} 的 {role} 端尚无任何记录")
            cell = roles[role]
            if not any(c.candidate_id == candidate_id for c in cell.candidates):
                raise ProbeFormatError("候选不存在或已不属于该端，无法选定")
            changed = cell.selected_id != candidate_id
            cell.selected_id = candidate_id
            if changed:
                gen = self._bump_generation_locked()
            else:
                gen = self.generation
            return {"changed": changed, "generation": gen}

    # ---------- 分析生命周期 ----------

    def start_analysis(self, limit_us: int) -> dict:
        """登记一次分析启动，返回 job 描述（含代次与输入快照签名）。"""
        with self._lock:
            calls = self._complete_calls_locked()
            if not calls:
                raise ProbeFormatError("尚无两端都已确定的调用，无法开始分析")
            self.generation += 1
            gen = self.generation
            job_id = self._next_id("job")
            self.analysis = {
                "state": "running",
                "generation": gen,
                "input_sig": None,       # 完成且对版后才写入
                "limit_us": limit_us,
                "result": None,
                "error": None,
                "job_id": job_id,
            }
            return {
                "job_id": job_id,
                "generation": gen,
                "limit_us": limit_us,
                "calls": [
                    solver.Call(c.call_id, c.caller, c.callee, c.t1, c.t2, c.t3, c.t4)
                    for c in calls
                ],
                "input_sig": self.input_signature_locked(),
            }

    def publish_result(
        self,
        job_id: str,
        generation: int,
        input_sig: tuple,
        limit_us: int,
        result: dict | None,
        error: str | None,
    ) -> bool:
        """后台分析完成后回交结果。代次或输入签名对不上（旧回包晚到）则拒绝发布。"""
        with self._lock:
            current_sig = self.input_signature_locked()
            stale = (
                generation != self.generation
                or input_sig != current_sig
                or self.analysis.get("job_id") != job_id
            )
            if stale:
                return False
            self.analysis = {
                "state": "done",
                "generation": generation,
                "input_sig": list(input_sig),
                "limit_us": limit_us,
                "result": result,
                "error": error,
                "job_id": job_id,
            }
            return True

    # ---------- 序列化 ----------

    def snapshot(self) -> dict:
        """供页面渲染的完整状态。"""
        with self._lock:
            calls_out = []
            complete = 0
            for call_id in sorted(self._cells.keys()):
                roles = self._cells[call_id]
                entry: dict = {"call_id": call_id, "roles": {}}
                both_selected = True
                for role in (ROLE_CALLER, ROLE_CALLEE):
                    cell = roles.get(role)
                    if cell is None:
                        entry["roles"][role] = {"present": False, "candidates": [], "selected_id": None}
                        both_selected = False
                        continue
                    entry["roles"][role] = {
                        "present": True,
                        "selected_id": cell.selected_id,
                        "needs_choice": len(cell.candidates) > 1 and cell.selected_id is None,
                        "candidates": [
                            {
                                "candidate_id": c.candidate_id,
                                "host": c.record.host,
                                "req_ts": c.record.req_ts,
                                "resp_ts": c.record.resp_ts,
                                "sources": list(c.sources),
                            }
                            for c in cell.candidates
                        ],
                    }
                    if cell.selected_id is None:
                        both_selected = False
                entry["status"] = "ready" if both_selected else "incomplete"
                if both_selected:
                    complete += 1
                entry["missing_roles"] = [
                    r for r in (ROLE_CALLER, ROLE_CALLEE) if not entry["roles"][r]["present"]
                ]
                entry["needs_choice_roles"] = [
                    r
                    for r in (ROLE_CALLER, ROLE_CALLEE)
                    if entry["roles"][r]["present"] and entry["roles"][r]["needs_choice"]
                ]
                calls_out.append(entry)

            hosts = sorted(self._all_hosts_locked())
            analysis = dict(self.analysis)
            if analysis.get("input_sig") is not None:
                analysis = {**analysis, "input_sig": list(analysis["input_sig"])}
            return {
                "board_id": self.board_id,
                "generation": self.generation,
                "files": list(self.files),
                "limits": {"max_calls": MAX_CALLS, "max_hosts": MAX_HOSTS, "max_files": MAX_FILES},
                "stats": {
                    "distinct_calls": len(calls_out),
                    "complete_calls": complete,
                    "hosts": len(hosts),
                    "files": len(self.files),
                },
                "hosts": hosts,
                "calls": calls_out,
                "analysis": analysis,
            }
