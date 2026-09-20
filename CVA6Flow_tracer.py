#!/usr/bin/env python3
"""CVA6Flow tracer: Verilator VCD of CVA6 -> JSON for the CVA6Flow viewer.

Reads a Verilator VCD of a cv64a6 simulation and follows every instruction
from fetch through decode, issue, execute, writeback and commit, with the
I-cache, D-cache, load-store unit, branch and forwarding events around it.
Writes one JSON, schema 3, described under "The JSON" in README.md.

The instruction text comes from the objdump listing of the test, picked up
beside the VCD as <name>.list or named with --disasm-list.

Usage:
    python3 CVA6Flow_tracer.py daxpy.vcd -o daxpy.json
    python3 CVA6Flow_tracer.py daxpy.vcd --disasm-list results/run/daxpy.list
    python3 CVA6Flow_tracer.py daxpy.vcd -o daxpy.json --strict --quiet
"""
import argparse
import bisect
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field


# ============================================================================
# 3. Schema and output constants
# ============================================================================

# Carried in metadata.schema_version. Bump it with any key added, removed or
# redefined.
SCHEMA_VERSION = 3
TOOL = "cva6flow_tracer"

# The field lists metadata carries, so the pages and the scripts clip, shift
# and scan records and events without keeping their own copies.
CYCLE_FIELDS = (
    "fe1_lo_cycle", "fe1_hi_cycle", "fe2_lo_cycle", "fe2_hi_cycle",
    "fe_out_cycle", "dec_cycle", "is_cycle", "ex_cycle", "wb_cycle",
    "co_cycle", "flush_cycle", "lsu_admit_cycle", "lsu_release_cycle",
    "bp_resolve_cycle",
)
CYCLE_LIST_FIELDS = ()
ID_FIELDS = (
    "fwd_rs1_producer_id", "fwd_rs2_producer_id", "fwd_rs3_producer_id",
    "pre_fetch_wait_shared_id", "bubble_causer_id", "bubble_shared_id",
    "caused_bubble_recovery_id",
)
ID_LIST_FIELDS = ()
EVENT_FIELDS = {
    "ic_events.access_cycles": "cycle",
    "ic_events.miss_cycles": "cycle",
    "ic_events.deliveries": ["fe1_cycle", "fe2_cycle"],
    "dc_events.access_cycles": "cycle",
    "dc_events.allocs": ["cycle"],
    "mem_writebacks": ["alloc_cycle", "send_cycle", "ack_cycle",
                       "evict_cycle"],
}
EVENT_TWIN_FIELDS = {}

METADATA_KEY_ORDER = (
    "tool", "schema_version", "vcd_path", "vcd_scope_prefix",
    "disasm_list_path", "config_name", "clock_period", "time_unit",
    "clock_period_source", "vcd_clock_period", "vcd_timescale", "dc_sets",
    "record_fields", "cycle_fields", "cycle_list_fields", "id_fields",
    "id_list_fields", "event_fields", "event_twin_fields", "degraded",
    "clipped", "stats",
)
TOP_LEVEL_ORDER = ("metadata", "config_params", "instructions", "ic_events",
                   "dc_events", "mem_writebacks")

# Written only under --emit-diagnostics: two lists per memory instruction
# that no page reads and that cost real bytes.
DIAGNOSTIC_ONLY_FIELDS = ("lsu_state_history", "dc_event_log")

# Scoreboard slots the forwarding producers are resolved from after the
# walk. A slot number is not a record, so they never reach the JSON.
INTERNAL_ONLY_FIELDS = ("fwd_rs1_tid", "fwd_rs2_tid", "fwd_rs3_tid")

# RTL names kept although their suffix reads as a record reference, since
# readers grep the RTL for them.
CORE_NAMES = ("trans_id",)

# A Verilator VCD advances two timescale units per cycle, whatever the clock,
# so time comes from an assumed 50 MHz instead of the VCD.
ASSUMED_CLOCK_PERIOD_PS = 20000
TIME_UNIT = "1ps"


# ============================================================================
# 4. Core constants
# ============================================================================

# Configuration. Values from cv64a6_imafdc_sv39_hpdcache_wb_config_pkg.sv and
# build_config_pkg.sv.
DEFAULT_CONFIG_NAME = "cv64a6_imafdc_sv39_hpdcache_wb"
DEFAULT_SCOPE_PREFIX = "TOP.ariane_testharness.i_ariane.i_cva6"

# Front end. Superscalar builds are refused, and every configuration here
# enables RVC, so a fetch is 32 bits holding up to two instructions.
FETCH_WIDTH = 32
FETCH_BYTES = FETCH_WIDTH // 8
FETCH_OFFSET_MASK = FETCH_BYTES - 1
INSTR_PER_FETCH = FETCH_WIDTH // 16

# Back end. NR_SB_ENTRIES and the port counts are the largest build this
# tracer accepts. The signal table is generated from the sizes the VCD
# declares.
NR_ISSUE_PORTS = 1
NR_COMMIT_PORTS = 2
NR_WB_PORTS = 5
NR_SB_ENTRIES = 8

# ex_stage's three D-cache request ports, CVA6-wide: 0 MMU and PTW, 1 load,
# 2 store (load_store_unit.sv:316, :587, :546).
DCACHE_REQ_PORTS = 3

# I-cache controller FSM, cva6_icache.sv:122, as the VCD's binary strings.
# A delivery in READ is a hit and one in MISS a line miss.
FSM_FLUSH = "000"
FSM_IDLE = "001"
FSM_READ = "010"
FSM_MISS = "011"
FSM_KILL_ATRANS = "100"
FSM_KILL_MISS = "101"
ICACHE_NON_READ_STATES = frozenset({
    FSM_FLUSH, FSM_IDLE, FSM_MISS, FSM_KILL_ATRANS, FSM_KILL_MISS, None,
})

# load_unit.sv:83, 9 states in 4 bits, and store_unit.sv:119, 4 in 2 bits.
LOAD_FSM_NAMES = {
    0: "IDLE",
    1: "WAIT_GNT",
    2: "SEND_TAG",
    3: "WAIT_PAGE_OFFSET",
    4: "ABORT_TRANSACTION",
    5: "ABORT_TRANSACTION_NI",
    6: "WAIT_TRANSLATION",
    7: "WAIT_FLUSH",
    8: "WAIT_WB_EMPTY",
}
STORE_FSM_NAMES = {
    0: "IDLE",
    1: "VALID_STORE",
    2: "WAIT_TRANSLATION",
    3: "WAIT_STORE_READY",
}
# The one busy state of each FSM that takes a second request without passing
# through IDLE (load_unit.sv:343, store_unit.sv:191).
LOAD_ADMIT_WHILE_BUSY = "SEND_TAG"
STORE_ADMIT_WHILE_BUSY = "VALID_STORE"

# cf_t, ariane_pkg.sv:170-176: the prediction each instruction carries and
# the type branch_unit resolves.
CF_T_NAMES = {
    0: "NoCF",
    1: "Branch",
    2: "Jump",
    3: "JumpR",
    4: "Return",
}

# fu_t, ariane_pkg.sv:188-201.
FU_NAME = {
    0: "NONE",
    1: "LOAD",
    2: "STORE",
    3: "ALU",
    4: "CTRL_FLOW",
    5: "MULT",
    6: "CSR",
    7: "FPU",
    8: "FPU_VEC",
    9: "CVXIF",
    10: "ACCEL",
    11: "AES",
}
# LOAD and STORE are Mem whether the target register is integer or FP, since
# telling them apart needs the op, which the tracer does not read.
FU_CATEGORY = {
    "ALU": "Int",
    "CTRL_FLOW": "Int",
    "MULT": "Int",
    "CSR": "Int",
    "AES": "Int",
    "LOAD": "Mem",
    "STORE": "Mem",
    "FPU": "FP",
    "FPU_VEC": "FP",
    "CVXIF": "CVXIF",
    "ACCEL": "ACCEL",
    "NONE": "None",
}

# HPDcache requester ids, cva6_hpdcache_subsystem.sv:181-189 with NumPorts 4
# (cva6.sv:380): 1 the load unit and 3 the store port.
HPDCACHE_NUM_PORTS = 4
LOAD_UNIT_SID = 1
STORE_PORT_SID = HPDCACHE_NUM_PORTS - 1

# Cycles from an access's release to its MSHR allocation, measured on daxpy:
# 2,065 of 2,070 store allocations sit at +4 (three cache stages plus one),
# and 2,047 of 2,047 otherwise unclaimed load allocations at +1.
HPDCACHE_STORE_LOOKAHEAD = 4
HPDCACHE_LOAD_LOOKAHEAD = 1

# hpdcache_miss_handler.sv:154 leaves the refill FSM typedef unsized, so the
# VCD holds 32 bits, and any value but 0 is a refill in progress.
REFILL_FSM_IDLE = 0

# A writeback joins its eviction within this many cycles of the allocation.
# On daxpy all 1,318 links share the cycle, so one cycle only absorbs skew.
EVICTION_JOIN_WINDOW = 1

# Synthesised fetch cycles sit this far below fe_out_cycle, one more for a
# wrapping instruction's second fetch.
FETCH_SYNTH_DEPTH = 2
FETCH_SYNTH_DEPTH_WRAP = 3

# The progress line is offered one line in this many plus one, a mask so
# the test stays cheap.
PROGRESS_LINE_MASK = 0x3FFF

# Missing signals listed before the report is cut short, and the candidate
# paths shown for each.
MAX_MISSING_REPORTED = 10
MAX_CANDIDATES_SHOWN = 5

# A clock period under this many picoseconds is not a real clock.
MIN_PLAUSIBLE_CLOCK_PS = 1000
TIMESCALE_TO_PS = {"fs": 1e-3, "ps": 1.0, "ns": 1e3, "us": 1e6, "ms": 1e9,
                   "s": 1e12}

# Signal table. Each row maps the RTL name a mechanism reads to its path
# under --scope-prefix and to the mechanisms needing it. A core signal is
# required, and every other mechanism resolves all or nothing.
SCOREBOARD = "issue_stage_i.i_scoreboard."
READ_OPERANDS = "issue_stage_i.i_issue_read_operands."
ICACHE = "gen_cache_hpd.i_cache_subsystem.i_cva6_icache."
HPDCACHE = "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
MISS_HANDLER = HPDCACHE + "hpdcache_miss_handler_i."
CORE = ("core",)
SIGNALS = {
    "clk_i": ("clk_i", CORE),
    "fetch_entry_valid_i": ("id_stage_i.fetch_entry_valid_i", CORE),
    "fetch_entry_ready_o": ("id_stage_i.fetch_entry_ready_o", CORE),
    "fetch_entry.address": ("fetch_entry_if_id[0].address", CORE),
    "fetch_entry.instruction": ("fetch_entry_if_id[0].instruction", CORE),
    "rvfi_is_compressed_o": ("id_stage_i.rvfi_is_compressed_o", CORE),
    # High while the realigner serves an instruction split across two
    # fetches, a cross-check of wraps_line for --verbose.
    "serving_unaligned_o": ("i_frontend.i_instr_realign.serving_unaligned_o",
                            CORE),
    "decoded_instr_valid_i": (SCOREBOARD + "decoded_instr_valid_i", CORE),
    "decoded_instr_ack_o": (SCOREBOARD + "decoded_instr_ack_o", CORE),
    "decoded_instr.fu": (SCOREBOARD + "decoded_instr_i[0].fu", CORE),
    "decoded_instr.rs1": (SCOREBOARD + "decoded_instr_i[0].rs1", CORE),
    "decoded_instr.rs2": (SCOREBOARD + "decoded_instr_i[0].rs2", CORE),
    "decoded_instr.rd": (SCOREBOARD + "decoded_instr_i[0].rd", CORE),
    "decoded_instr.bp.cf": (SCOREBOARD + "decoded_instr_i[0].bp.cf", CORE),
    "decoded_instr.bp.predict_address": (
        SCOREBOARD + "decoded_instr_i[0].bp.predict_address", CORE),
    # While high the decode handshake still fires but no slot is allocated
    # (scoreboard.sv:171), so counting it would drift every later trans_id.
    "flush_unissued_instr_i": (SCOREBOARD + "flush_unissued_instr_i", CORE),
    "issue_pointer_q": (SCOREBOARD + "issue_pointer_q", CORE),
    "wt_valid_i": (SCOREBOARD + "wt_valid_i", CORE),
    "commit_ack_o": ("commit_stage_i.commit_ack_o", CORE),
    "flush_ctrl_if": ("flush_ctrl_if", CORE),
    "flush_ctrl_ex": ("flush_ctrl_ex", CORE),
    # Perf-counter event 16 (perf_counters.sv:126).
    "icache_dreq_o.req": ("i_frontend.icache_dreq_o.req", CORE),
    "icache_dreq_i.valid": ("i_frontend.icache_dreq_i.valid", ("icache",)),
    "icache_dreq_i.vaddr": ("i_frontend.icache_dreq_i.vaddr", ("icache",)),
    "icache_dreq_o.kill_s2": ("i_frontend.icache_dreq_o.kill_s2",
                              ("icache",)),
    "icache.state_q": (ICACHE + "state_q", ("icache",)),
    # One high cycle per accepted cacheable ifill (cva6_icache.sv:301-303),
    # perf-counter event 1, wrong-path fills included.
    "icache.miss_o": (ICACHE + "miss_o", ("icache",)),
    "load_unit.state_q": ("ex_stage_i.lsu_i.i_load_unit.state_q",
                          ("lsu_fsm",)),
    "store_unit.state_q": ("ex_stage_i.lsu_i.i_store_unit.state_q",
                           ("lsu_fsm",)),
    # The request feeding both FSMs (load_store_unit.sv:174). Its trans_id
    # on the cycle before an FSM leaves IDLE names the admitted record.
    "lsu_ctrl.trans_id": ("ex_stage_i.lsu_i.lsu_ctrl.trans_id",
                          ("lsu_fsm",)),
    "lsu_bypass.pop_ld_i": ("ex_stage_i.lsu_i.lsu_bypass_i.pop_ld_i",
                            ("lsu_fsm",)),
    "lsu_bypass.pop_st_i": ("ex_stage_i.lsu_i.lsu_bypass_i.pop_st_i",
                            ("lsu_fsm",)),
    "mshr_alloc_i": (MISS_HANDLER + "mshr_alloc_i",
                     ("dcache", "mem_writeback")),
    "mshr_alloc_tid_i": (MISS_HANDLER + "mshr_alloc_tid_i", ("dcache",)),
    "mshr_alloc_sid_i": (MISS_HANDLER + "mshr_alloc_sid_i", ("dcache",)),
    "mshr_alloc_is_prefetch_i": (MISS_HANDLER + "mshr_alloc_is_prefetch_i",
                                 ("dcache",)),
    "mshr_alloc_nline_i": (MISS_HANDLER + "mshr_alloc_nline_i",
                           ("dcache", "mem_writeback")),
    # A check with a hit on the same cycle joins an MSHR already open for the
    # line, which is how loads following stores to one line miss.
    "mshr_check_i": (MISS_HANDLER + "mshr_check_i", ("dcache",)),
    "mshr_check_nline_i": (MISS_HANDLER + "mshr_check_nline_i", ("dcache",)),
    "mshr_check_hit_o": (MISS_HANDLER + "mshr_check_hit_o", ("dcache",)),
    # Busy while a refill holds the cache port, stalling unrelated loads.
    "refill_fsm_q": (MISS_HANDLER + "refill_fsm_q", ("dcache",)),
    "refill_core_rsp_valid_o": (MISS_HANDLER + "refill_core_rsp_valid_o",
                                ("dcache",)),
    "refill_core_rsp_o.tid": (MISS_HANDLER + "refill_core_rsp_o.tid",
                              ("dcache",)),
    # Dirty-line writebacks with the write buffer configured out: flush_alloc
    # hands the line over, the flush channel's request and response write it.
    "flush_alloc": (HPDCACHE + "flush_alloc", ("mem_writeback",)),
    "flush_alloc_ready": (HPDCACHE + "flush_alloc_ready", ("mem_writeback",)),
    "flush_alloc_nline": (HPDCACHE + "flush_alloc_nline", ("mem_writeback",)),
    "flush_alloc_way": (HPDCACHE + "flush_alloc_way", ("mem_writeback",)),
    # hpdcache.sv:981 merges the two and :1258 asserts they never overlap,
    # which tells an eviction from a CMO flush.
    "ctrl_flush_alloc": (HPDCACHE + "ctrl_flush_alloc", ("mem_writeback",)),
    "cmo_flush_alloc": (HPDCACHE + "cmo_flush_alloc", ("mem_writeback",)),
    "mem_req_write_flush_valid": (HPDCACHE + "mem_req_write_flush_valid",
                                  ("mem_writeback",)),
    "mem_req_write_flush_ready": (HPDCACHE + "mem_req_write_flush_ready",
                                  ("mem_writeback",)),
    "mem_req_write_flush.mem_req_id": (
        HPDCACHE + "mem_req_write_flush.mem_req_id", ("mem_writeback",)),
    "mem_req_write_flush.mem_req_addr": (
        HPDCACHE + "mem_req_write_flush.mem_req_addr", ("mem_writeback",)),
    "mem_resp_write_flush_valid": (HPDCACHE + "mem_resp_write_flush_valid",
                                   ("mem_writeback",)),
    "mem_resp_write_flush_ready": (HPDCACHE + "mem_resp_write_flush_ready",
                                   ("mem_writeback",)),
    "mem_resp_write_flush.mem_resp_w_id": (
        HPDCACHE + "mem_resp_write_flush.mem_resp_w_id", ("mem_writeback",)),
    "flush_ack_nline": (HPDCACHE + "flush_ack_nline", ("mem_writeback",)),
    # The miss allocation that evicted the line: the join to its writeback is
    # on (set, victim way), set being the nline masked by the set count.
    "mshr_alloc_wback_i": (MISS_HANDLER + "mshr_alloc_wback_i",
                           ("mem_writeback",)),
    "mshr_alloc_victim_way_i": (MISS_HANDLER + "mshr_alloc_victim_way_i",
                                ("mem_writeback",)),
    # bp_resolve_t (cva6.sv:134), valid for one cycle at the branch's
    # ex_cycle, or just after under contention.
    "resolved_branch_i.valid": (SCOREBOARD + "resolved_branch_i.valid",
                                ("bp_resolution",)),
    "resolved_branch_i.pc": (SCOREBOARD + "resolved_branch_i.pc",
                             ("bp_resolution",)),
    "resolved_branch_i.target_address": (
        SCOREBOARD + "resolved_branch_i.target_address", ("bp_resolution",)),
    "resolved_branch_i.is_taken": (SCOREBOARD + "resolved_branch_i.is_taken",
                                   ("bp_resolution",)),
    "resolved_branch_i.is_mispredict": (
        SCOREBOARD + "resolved_branch_i.is_mispredict", ("bp_resolution",)),
    "resolved_branch_i.cf_type": (SCOREBOARD + "resolved_branch_i.cf_type",
                                  ("bp_resolution",)),
    # issue_read_operands.sv:175, :180 and :185 declare idx_hzd_rs1 to rs3,
    # and :215 forward_rs1 to rs3.
    "forward_rs1": (READ_OPERANDS + "forward_rs1", ("forwarding",)),
    "forward_rs2": (READ_OPERANDS + "forward_rs2", ("forwarding",)),
    "forward_rs3": (READ_OPERANDS + "forward_rs3", ("forwarding",)),
    "idx_hzd_rs1": (READ_OPERANDS + "idx_hzd_rs1[0]", ("forwarding",)),
    "idx_hzd_rs2": (READ_OPERANDS + "idx_hzd_rs2[0]", ("forwarding",)),
    "idx_hzd_rs3": (READ_OPERANDS + "idx_hzd_rs3[0]", ("forwarding",)),
}

# The registered scoreboard entry, written at the decode and issue handshake
# and stable until commit, bar the target DebugEn overwrites at writeback.
# Generated per slot by signal_table.
MEM_Q_FIELDS = ("fu", "rs1", "rs2", "rd", "bp.cf", "bp.predict_address")

# Build-size probes, over every header path, so a build larger than the
# table above is refused rather than half tracked.
MEM_Q_SLOT_PROBE = re.compile(r"mem_q\[(\d+)\]\.sbe\.fu(?:\[[\d:]+\])?$")
DECODED_PORT_PROBE = re.compile(
    r"decoded_instr_i\[(\d+)\]\.fu(?:\[[\d:]+\])?$")
COMMIT_PORT_PROBE = re.compile(r"commit_pointer_q\[(\d+)\](?:\[[\d:]+\])?$")
WB_PORT_PROBE = re.compile(r"trans_id_i\[(\d+)\](?:\[[\d:]+\])?$")

# hpdcache_set_t, setWidth bits (hpdcache_miss_handler.sv:107), where
# setWidth is $clog2(sets) (hpdcache_pkg.sv:493).
DCACHE_SET_SIGNAL = MISS_HANDLER + "refill_set_o"
HPDCACHE_SCOPE = "gen_cache_hpd."

BIT_RANGE = re.compile(r"\[\d+:\d+\]$")

# An instruction line of an objdump -d -S -l listing: indented, the address,
# a colon, the word and the text. The indent keeps the address labels out, and
# source lines practically never start with hex, a colon and hex.
DISASM_LINE = re.compile(r"^\s+([0-9a-fA-F]+):\s+([0-9a-fA-F]+)\s+(.+)$")


# ============================================================================
# 5. Progress and logging
# ============================================================================

# SHARED BEGIN py-progress

# Needs: sys, time

PROGRESS_INTERVAL_S = 0.25
PLAIN_PROGRESS_INTERVAL_S = 5.0


class Progress:
    """One progress line on stderr: rewritten in place on a terminal, a new
    line every few seconds in a log, and nothing when quiet."""

    def __init__(self, label, total_bytes=0, quiet=False):
        self.label = label
        self.total_bytes = total_bytes
        self.live = not quiet and sys.stderr.isatty()
        self.plain = not quiet and not sys.stderr.isatty()
        self.start = time.time()
        self.last_emit = 0.0
        self.last_plain = self.start
        self.lines = 0
        self.records = 0
        self.bytes_done = 0

    def update(self, lines, records=0, bytes_done=0):
        self.lines, self.records, self.bytes_done = lines, records, bytes_done
        now = time.time()
        if now - self.last_emit < PROGRESS_INTERVAL_S:
            return
        self.last_emit = now
        if self.live:
            sys.stderr.write("\r" + self.message(now) + "   ")
            sys.stderr.flush()
        elif (self.plain
              and now - self.last_plain >= PLAIN_PROGRESS_INTERVAL_S):
            self.last_plain = now
            sys.stderr.write(self.message(now) + "\n")
            sys.stderr.flush()

    def message(self, now):
        # A count not known yet is left out rather than printed as zero.
        parts = [f"{self.lines:,} lines"]
        if self.records:
            parts.append(f"{self.records:,} instructions")
        if self.total_bytes and self.bytes_done:
            share = min(100, int(100 * self.bytes_done / self.total_bytes))
            parts.append(f"{share}%")
        parts.append(f"{now - self.start:.1f}s")
        return f"[{self.label}] " + ", ".join(parts)

    def done(self):
        """The final line, printed once."""
        if self.live:
            sys.stderr.write("\r" + self.message(time.time()) + "   \n")
        elif self.plain:
            sys.stderr.write(self.message(time.time()) + "\n")
        sys.stderr.flush()

# SHARED END py-progress


# SHARED BEGIN py-log

# Needs: sys


def log_info(message):
    print(f"[INFO] {message}", file=sys.stderr)


def log_warn(message):
    print(f"[WARN] {message}", file=sys.stderr)


def log_error(message):
    print(f"[ERROR] {message}", file=sys.stderr)

# SHARED END py-log


# SHARED BEGIN py-human-size

# Needs: none


def human(size):
    """A size in bytes as whole B, or as KiB, MiB or GiB with one decimal."""
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"

# SHARED END py-human-size


# ============================================================================
# 6. Input reader
# ============================================================================

def strip_bit_range(path):
    while True:
        stripped = BIT_RANGE.sub("", path)
        if stripped == path:
            return path
        path = stripped


def parse_var_block(f):
    """Read the VCD header up to $enddefinitions. Returns (path_to_id,
    path_width, timescale), path_width holding each declared size so a width
    such as the D-cache set index can be read back. Verilator writes $var
    lines with and without a separate bit-range token, and both are read."""
    scope_stack = []
    path_to_id = {}
    path_width = {}
    timescale = None
    for line in f:
        line = line.strip()
        if not line:
            continue
        if line.startswith("$enddefinitions"):
            break
        if line.startswith("$scope"):
            tokens = line.split()
            if len(tokens) >= 3:
                scope_stack.append(tokens[2])
        elif line.startswith("$upscope"):
            if scope_stack:
                scope_stack.pop()
        elif line.startswith("$timescale"):
            rest = line[len("$timescale"):].split("$end")[0].strip()
            if rest:
                timescale = rest
        elif line.startswith("$var"):
            tokens = line.split()
            if len(tokens) < 6:
                continue
            name = tokens[4]
            if len(tokens) >= 7 and tokens[5] != "$end":
                name += tokens[5]
            full_path = ".".join(scope_stack + [name])
            path_to_id[full_path] = tokens[3]
            if tokens[2].isdigit():
                path_width[full_path] = int(tokens[2])
    return path_to_id, path_width, timescale


def signal_table(nr_sb_entries, nr_commit_ports, nr_wb_ports):
    """SIGNALS plus the per-slot and per-port rows of the build the probes
    found, so a smaller sweep build requires only the slots it has."""
    table = dict(SIGNALS)
    for port in range(DCACHE_REQ_PORTS):
        table[f"dcache_req_ports_o[{port}].data_req"] = (
            f"ex_stage_i.dcache_req_ports_o[{port}].data_req", CORE)
    for port in range(nr_wb_ports):
        table[f"trans_id_i[{port}]"] = (
            SCOREBOARD + f"trans_id_i[{port}]", CORE)
    for port in range(nr_commit_ports):
        table[f"commit_pointer_q[{port}]"] = (
            SCOREBOARD + f"commit_pointer_q[{port}]", CORE)
    for slot in range(nr_sb_entries):
        for name in MEM_Q_FIELDS:
            table[f"mem_q[{slot}].{name}"] = (
                SCOREBOARD + f"mem_q[{slot}].sbe.{name}", CORE)
    return table


def match_signal_table(table, path_to_id, scope_prefix):
    """(ids, missing): the VCD id of every table row found exactly once
    under the scope prefix, and the rows found never or more than once."""
    by_stripped = defaultdict(list)
    for full_path, vcd_id in path_to_id.items():
        by_stripped[strip_bit_range(full_path)].append(vcd_id)
    ids, missing = {}, []
    for name, (path, _mechanisms) in table.items():
        hits = by_stripped.get(f"{scope_prefix}.{path}" if scope_prefix
                               else path, [])
        if len(hits) == 1:
            ids[name] = hits[0]
        else:
            missing.append(name)
    return ids, missing


def resolved_mechanisms(table, ids):
    """Mechanism name to whether every signal it needs was found."""
    resolved = {}
    for name, (_path, mechanisms) in table.items():
        for mechanism in mechanisms:
            resolved[mechanism] = resolved.get(mechanism, True) and (
                name in ids)
    return resolved


def probe_max_index(path_to_id, pattern):
    """The largest index pattern captures in any header path, or -1."""
    largest = -1
    for path in path_to_id:
        match = pattern.search(path)
        if match:
            largest = max(largest, int(match.group(1)))
    return largest


def probe_dcache_sets(path_width, scope_prefix):
    """The D-cache set count from the width of refill_set_o, or None when
    the VCD does not declare it."""
    by_stripped = {strip_bit_range(p): w for p, w in path_width.items()}
    path = (f"{scope_prefix}.{DCACHE_SET_SIGNAL}" if scope_prefix
            else DCACHE_SET_SIGNAL)
    width = by_stripped.get(path)
    return 1 << width if width else None


def refuse(what, found, limit, constant, needed):
    """Say why a build larger than this tracer's tables is refused, and
    return the exit status."""
    log_error(f"The VCD has {what} up to index {found}, and this tracer "
              f"handles {limit} ({constant}). The rest would go untracked, "
              f"so the JSON would be silently wrong.")
    print(f"        Set {constant} near the top of CVA6Flow_tracer.py to "
          f"{needed} and rerun.", file=sys.stderr)
    return 2


def report_missing(table, missing, path_to_id):
    """List the table rows the VCD lacks, with candidate paths, capped so
    one wrong scope prefix does not bury the diagnosis."""
    if not missing:
        return
    log_warn(f"{len(missing)} signal(s) of the table are not in the VCD:")
    for name in missing[:MAX_MISSING_REPORTED]:
        path, mechanisms = table[name]
        # The last segment without its index, to search candidates by.
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        print(f"         {path} ({', '.join(mechanisms)})", file=sys.stderr)
        candidates = [p for p in path_to_id if leaf in p]
        for candidate in candidates[:MAX_CANDIDATES_SHOWN]:
            print(f"             candidate: {candidate}", file=sys.stderr)
        if len(candidates) > MAX_CANDIDATES_SHOWN:
            print(f"             and {len(candidates) - MAX_CANDIDATES_SHOWN}"
                  f" more", file=sys.stderr)
    if len(missing) > MAX_MISSING_REPORTED:
        print(f"         and {len(missing) - MAX_MISSING_REPORTED:,} more. "
              f"This many usually means --scope-prefix is wrong rather "
              f"than the signals being absent.", file=sys.stderr)


def get_bit(binary_str, bit_idx):
    """Bit bit_idx counted from the LSB, or None when the VCD holds x or z
    there. A short value is not unknown: VCD left-truncates, and IEEE 1364
    extends with the leftmost character, so 0 or 1 extends with 0."""
    if not binary_str:
        return 0
    if len(binary_str) > bit_idx:
        char = binary_str[-(bit_idx + 1)].lower()
    else:
        char = binary_str[0].lower()
        if char not in ("x", "z"):
            return 0
    if char in ("x", "z"):
        return None
    return 1 if char == "1" else 0


def binary_to_int(s):
    if not s or any(c in "xXzZ" for c in s):
        return None
    try:
        return int(s, 2)
    except ValueError:
        return None


def binary_to_hex(s):
    value = binary_to_int(s)
    return None if value is None else f"0x{value:x}"


def onehot_to_index(value):
    """The bit index of a one-hot value, None when it is not one-hot."""
    if value is None or value <= 0 or value & (value - 1):
        return None
    return value.bit_length() - 1


def cf_name(value):
    """A cf_t value's name, UNK_<n> outside the enum, None when unknown."""
    return None if value is None else CF_T_NAMES.get(value, f"UNK_{value}")


def fu_name(value):
    return None if value is None else FU_NAME.get(value, f"UNK_{value}")


def pc_value(rec):
    """A record's PC as an int, None when the VCD held x in it."""
    return None if rec.pc is None else int(rec.pc, 16)


def timescale_ps(timescale):
    """Picoseconds per timescale unit, None when it does not parse."""
    match = re.fullmatch(r"(\d+)\s*(fs|ps|ns|us|ms|s)", timescale or "")
    if not match:
        return None
    return int(match.group(1)) * TIMESCALE_TO_PS[match.group(2)]


# ============================================================================
# 7. Event extraction
# ============================================================================

@dataclass
class InstructionRecord:
    """One fetched instruction. The fields up to n_caused_bubble_flushed are
    the JSON record, in the order the JSON writes them."""

    id: int
    pc: str
    instr_word: str
    disasm: str = None
    is_compressed: bool = False
    fu: str = None
    fu_category: str = None
    rd: int = None
    rs1: int = None
    rs2: int = None
    trans_id: int = None
    # A 32-bit instruction at the last 2-byte slot of its fetch block, so its
    # high half arrives in the next fetch. Set on flushed records too, since
    # the realigner combines the halves whether or not the instruction commits.
    wraps_line: bool = False
    fe1_lo_cycle: int = None
    fe1_hi_cycle: int = None
    fe2_lo_cycle: int = None
    fe2_hi_cycle: int = None
    # state_q == MISS at the delivery. Null when the fetch cycles were
    # synthesised, since nothing was observed.
    ic_miss_lo: bool = None
    ic_miss_hi: bool = None
    fe_out_cycle: int = None
    # Decode and issue are one handshake (scoreboard.sv:151), so they share a
    # cycle, and no dumped signal marks entry to execute, hence is_cycle + 1.
    dec_cycle: int = None
    is_cycle: int = None
    ex_cycle: int = None
    wb_cycle: int = None
    co_cycle: int = None
    flushed: bool = False
    flush_reason: str = None
    flush_cycle: int = None
    # The load or store FSM left IDLE for this record, usually is_cycle + 1
    # and later under stalls or a TLB miss.
    lsu_admit_cycle: int = None
    # One cycle before the FSM returned to IDLE, so windows never share a
    # cycle. A load's data arrives later through ldbuf, so this is a release.
    lsu_release_cycle: int = None
    dc_miss: bool = None
    dc_coalesced: bool = None
    dc_refill_overlap: bool = None
    lsu_state_history: list = None
    dc_event_log: list = None
    fwd_rs1_used: bool = False
    fwd_rs1_via: str = None
    fwd_rs1_producer_id: int = None
    fwd_rs2_used: bool = False
    fwd_rs2_via: str = None
    fwd_rs2_producer_id: int = None
    fwd_rs3_used: bool = False
    fwd_rs3_via: str = None
    fwd_rs3_producer_id: int = None
    # Read from decoded_instr_i at issue, then from mem_q at writeback and
    # commit. cf_t names the predictor: Branch the BHT, JumpR the BTB, Return
    # the RAS, Jump the predecoder, NoCF none or not taken.
    bp_predicted_cf: str = None
    bp_predicted_target: int = None
    bp_resolved_cf: str = None
    bp_resolved_target: int = None
    bp_resolved_taken: bool = None
    bp_mispredict: bool = None
    bp_resolve_cycle: int = None
    bp_outcome: str = None
    bp_decided_at: str = None
    synthesised_stages: list = field(default_factory=list)
    n_pre_fetch_wait_cycles: int = None
    pre_fetch_wait_shared_id: int = None
    # pred_taken, unpred, mispred, or flush for a CSR write, a fence, an
    # atomic commit or an exception entry.
    bubble_kind: str = None
    bubble_causer_id: int = None
    n_bubble_cycles: int = None
    bubble_shared_id: int = None
    n_bubble_gap_cycles: int = None
    caused_bubble_kind: str = None
    caused_bubble_recovery_id: int = None
    n_caused_bubble_cycles: int = None
    n_caused_bubble_flushed: int = None
    fwd_rs1_tid: int = None
    fwd_rs2_tid: int = None
    fwd_rs3_tid: int = None


class Run:
    """One conversion's settings and what its walk measured, passed around
    in place of module globals so the tracer can run twice in one process."""

    def __init__(self, verbose=False, progress=None):
        self.verbose = verbose
        self.progress = progress
        self.n_lines = 0
        self.n_changes = 0
        self.n_cycles = 0
        self.last_ts = 0
        self.first_edge_ts = None
        self.vcd_clock_period = None
        self.ends_cleanly = True


class EdgeSample:
    """The VCD values at one rising edge, read by signal-table name. It holds
    the walker's own state dict, so one sample serves every edge."""

    def __init__(self, state, ids):
        self.state = state
        self.ids = ids
        self.n_unknown_bit_reads = 0

    def get(self, name):
        return self.state.get(self.ids.get(name))

    def bit(self, name, index):
        value = get_bit(self.get(name), index)
        if value is None:
            # A strobe read as x drops the commit or writeback it carried, so
            # the count says records are missing, not merely untimed.
            self.n_unknown_bit_reads += 1
        return value


@dataclass
class ICacheEvent:
    fe1_cycle: int
    fe2_cycle: int
    vaddr_word: int
    is_miss: bool


class ICacheTimeline:
    """One ICacheEvent per delivered fetch. An access starts when the
    response address changes or state_q re-enters READ, the second catching a
    fetch of the same address again."""

    def __init__(self):
        self.events = []
        self.last_vaddr = None
        self.last_state = None
        self.access_start_cycle = None

    def on_edge(self, cycle, sample):
        state = sample.get("icache.state_q")
        vaddr = sample.get("icache_dreq_i.vaddr")
        if vaddr != self.last_vaddr or (
                state == FSM_READ
                and self.last_state in ICACHE_NON_READ_STATES):
            self.access_start_cycle = cycle
        self.last_vaddr = vaddr
        self.last_state = state
        if (sample.get("icache_dreq_i.valid") != "1"
                or sample.get("icache_dreq_o.kill_s2") == "1"):
            return
        word = binary_to_int(vaddr)
        if word is None:
            return
        start = (self.access_start_cycle
                 if self.access_start_cycle is not None else cycle)
        self.events.append(ICacheEvent(
            fe1_cycle=max(0, start - 1), fe2_cycle=cycle,
            vaddr_word=word & ~FETCH_OFFSET_MASK, is_miss=state == FSM_MISS))


class PerfCounterSamples:
    """The cycles the perf-counter event inputs 1, 16 and 17 were high, so a
    page counts any range the way the counters do, and the realigner's
    unaligned runs for --verbose."""

    def __init__(self, icache):
        self.icache = icache
        self.ic_access_cycles = []
        self.dc_access_cycles = []
        self.ic_miss_cycles = []
        self.n_unaligned_runs = 0
        self.n_unaligned_cycles = 0
        self.last_unaligned = None
        self.dc_request_names = [f"dcache_req_ports_o[{port}].data_req"
                                 for port in range(DCACHE_REQ_PORTS)]

    def on_edge(self, cycle, sample):
        # A value read after the edge holds for the cycle starting there,
        # which the counter adds at the next edge.
        if sample.get("icache_dreq_o.req") == "1":
            self.ic_access_cycles.append(cycle)
        if any(sample.get(name) == "1" for name in self.dc_request_names):
            self.dc_access_cycles.append(cycle)
        if self.icache and sample.get("icache.miss_o") == "1":
            self.ic_miss_cycles.append(cycle)
        unaligned = sample.get("serving_unaligned_o")
        if unaligned == "1":
            self.n_unaligned_cycles += 1
            if self.last_unaligned != "1":
                self.n_unaligned_runs += 1
        self.last_unaligned = unaligned


def spans_fetch_blocks(pc, is_compressed):
    """A 32-bit instruction at offset FETCH_BYTES - 2 of its fetch block has
    its high half in the next block, which instr_realign.sv combines and
    flags with serving_unaligned_o (line 63). The two match except on a
    record flushed mid-realignment."""
    if is_compressed or pc is None:
        return False
    return (int(pc, 16) & FETCH_OFFSET_MASK) == FETCH_BYTES - 2


class PipelineTracker:
    """The in-flight records and the handshake and flush events applied to
    them. fetched is a FIFO in program order, issued maps a trans_id to its
    record, and completed collects every record that left the pipeline."""

    def __init__(self, nr_sb_entries, nr_commit_ports, nr_wb_ports,
                 forwarding):
        self.nr_sb_entries = nr_sb_entries
        self.nr_commit_ports = nr_commit_ports
        self.nr_wb_ports = nr_wb_ports
        self.forwarding = forwarding
        self.mem_q_names = [[f"mem_q[{slot}].{name}" for name in MEM_Q_FIELDS]
                            for slot in range(nr_sb_entries)]
        self.fetched = deque()
        self.issued = {}
        self.completed = []
        self.next_id = 1
        self.n_committed = 0
        # Flushed by the core. Records still in flight when the VCD ended
        # are drained instead, or one real flush would count twice.
        self.n_flushed_fetched = 0
        self.n_flushed_issued = 0
        self.n_drained_fetched = 0
        self.n_drained_issued = 0
        self.n_unmatched_writebacks = 0
        self.n_discarded_writebacks = 0
        self.flushed_tids = set()
        self.flushed_tids_cycle = None
        self.n_unmatched_commits = 0
        self.n_unmatched_decodes = 0
        self.n_unmatched_resolutions = 0
        self.n_issue_cycles = 0
        self.n_issue_cycles_with_any_wb = 0
        self.wb_bus_tids = set()
        self.last_flush_if = "0"
        self.last_flush_ex = "0"

    def read_mem_q(self, sample, tid):
        """fu, rs1, rs2, rd, bp.cf and bp.predict_address of slot tid, all
        None for a slot past the build's scoreboard."""
        if not 0 <= tid < self.nr_sb_entries:
            return [None] * len(MEM_Q_FIELDS)
        return [binary_to_int(sample.get(name))
                for name in self.mem_q_names[tid]]

    def apply_mem_q(self, rec, mem_q, with_target):
        fu, rs1, rs2, rd, cf, target = mem_q
        if fu is not None:
            rec.fu = fu_name(fu)
            rec.fu_category = FU_CATEGORY.get(rec.fu, "Unknown")
        if rs1 is not None:
            rec.rs1 = rs1
        if rs2 is not None:
            rec.rs2 = rs2
        if rd is not None:
            rec.rd = rd
        if cf is not None:
            rec.bp_predicted_cf = cf_name(cf)
        if rec.bp_predicted_cf == "NoCF":
            rec.bp_predicted_target = None
        elif (with_target and target is not None
              and rec.bp_predicted_cf is not None):
            rec.bp_predicted_target = target

    def on_commit_edge(self, cycle, sample):
        for port in range(self.nr_commit_ports):
            if sample.bit("commit_ack_o", port) != 1:
                continue
            tid = binary_to_int(sample.get(f"commit_pointer_q[{port}]"))
            if tid is None:
                continue
            rec = self.issued.pop(tid, None)
            if rec is None:
                self.n_unmatched_commits += 1
                continue
            rec.co_cycle = cycle
            # DebugEn writes the resolved target over bp.predict_address at
            # writeback (scoreboard.sv:211-213), so only a record that never
            # wrote back still holds its prediction here.
            self.apply_mem_q(rec, self.read_mem_q(sample, tid),
                             with_target=rec.wb_cycle is None)
            self.completed.append(rec)
            self.n_committed += 1

    def on_flush_edge(self, cycle, sample):
        flush_if = sample.get("flush_ctrl_if") or "0"
        flush_ex = sample.get("flush_ctrl_ex") or "0"
        # EX first, since it also empties the fetched queue. On cv64a6
        # flush_ctrl_if rises alone on a mispredict (controller.sv:117).
        if flush_ex == "1" and self.last_flush_ex == "0":
            self.flush_fetched("flush_ex_fetched", cycle)
            self.flush_issued("flush_ex_issued", cycle)
        if flush_if == "1" and self.last_flush_if == "0":
            self.flush_fetched("flush_if", cycle)
        self.last_flush_if, self.last_flush_ex = flush_if, flush_ex

    def on_writeback_edge(self, cycle, sample):
        self.wb_bus_tids = set()
        for port in range(self.nr_wb_ports):
            if sample.bit("wt_valid_i", port) != 1:
                continue
            tid = binary_to_int(sample.get(f"trans_id_i[{port}]"))
            if tid is None:
                continue
            self.wb_bus_tids.add(tid)
            rec = self.issued.get(tid)
            if rec is None:
                # scoreboard.sv clears every slot after the writeback when
                # flush_i is high, so a writeback into a slot this edge
                # flushed is dropped by the core, not lost by the tracer.
                if cycle == self.flushed_tids_cycle and \
                        tid in self.flushed_tids:
                    self.n_discarded_writebacks += 1
                else:
                    self.n_unmatched_writebacks += 1
                continue
            first = rec.wb_cycle is None
            if first:
                rec.wb_cycle = cycle
            # Before this edge's write the slot still holds what issue
            # registered, prediction included.
            self.apply_mem_q(rec, self.read_mem_q(sample, tid),
                             with_target=first)

    def on_issue_edge(self, cycle, sample):
        """The decode and issue handshake, one event since issue_instr_o
        passes decoded_instr_i straight through (scoreboard.sv:151). Every
        field is read after the edge, like the handshake, which daxpy.vcd
        confirms on all 42,842 issues against mem_q."""
        if (sample.get("decoded_instr_valid_i") != "1"
                or sample.get("decoded_instr_ack_o") != "1"
                or sample.get("flush_unissued_instr_i") == "1"):
            return
        tid = binary_to_int(sample.get("issue_pointer_q"))
        if tid is None:
            return
        if not self.fetched:
            self.n_unmatched_decodes += 1
            return
        self.n_issue_cycles += 1
        if self.wb_bus_tids:
            self.n_issue_cycles_with_any_wb += 1
        rec = self.fetched.popleft()
        rec.dec_cycle = rec.is_cycle = cycle
        rec.ex_cycle = cycle + 1
        rec.trans_id = tid
        fu = binary_to_int(sample.get("decoded_instr.fu"))
        if fu is not None:
            rec.fu = fu_name(fu)
            rec.fu_category = FU_CATEGORY.get(rec.fu, "Unknown")
        rec.rs1 = binary_to_int(sample.get("decoded_instr.rs1"))
        rec.rs2 = binary_to_int(sample.get("decoded_instr.rs2"))
        rec.rd = binary_to_int(sample.get("decoded_instr.rd"))
        rec.bp_predicted_cf = cf_name(
            binary_to_int(sample.get("decoded_instr.bp.cf")))
        if rec.bp_predicted_cf not in (None, "NoCF"):
            rec.bp_predicted_target = binary_to_int(
                sample.get("decoded_instr.bp.predict_address"))
        if self.forwarding:
            self.capture_forwarding(rec, sample)
        self.issued[tid] = rec

    def capture_forwarding(self, rec, sample):
        # A producer writing back on this cycle forwards off the writeback
        # bus, and one already written back from its valid scoreboard slot
        # (issue_read_operands.sv:524-535).
        for n in (1, 2, 3):
            if sample.get(f"forward_rs{n}") != "1":
                continue
            tid = binary_to_int(sample.get(f"idx_hzd_rs{n}"))
            setattr(rec, f"fwd_rs{n}_used", True)
            setattr(rec, f"fwd_rs{n}_tid", tid)
            setattr(rec, f"fwd_rs{n}_via",
                    "wb" if tid in self.wb_bus_tids else "sb")

    def on_fetch_edge(self, cycle, sample):
        if (sample.get("fetch_entry_valid_i") != "1"
                or sample.get("fetch_entry_ready_o") != "1"):
            return
        # id_stage.sv:444 discards the entry while flush_i, which cva6.sv:719
        # wires to flush_ctrl_if, so queuing it would put every later issue
        # one record ahead.
        dropped = sample.get("flush_ctrl_if") == "1"
        rec = self.new_record(cycle, sample, dropped)
        if dropped:
            self.completed.append(rec)
            self.n_flushed_fetched += 1
        else:
            self.fetched.append(rec)

    def new_record(self, cycle, sample, dropped):
        pc = binary_to_hex(sample.get("fetch_entry.address"))
        word = binary_to_hex(sample.get("fetch_entry.instruction"))
        compressed = sample.get("rvfi_is_compressed_o") == "1"
        # fetch_entry.instruction is 32 bits wide, and a compressed
        # instruction sits in its low 16.
        if compressed and word is not None:
            word = f"0x{int(word, 16) & 0xFFFF:04x}"
        rec = InstructionRecord(
            id=self.next_id, pc=pc, instr_word=word, is_compressed=compressed,
            wraps_line=spans_fetch_blocks(pc, compressed), fe_out_cycle=cycle)
        if dropped:
            rec.flushed = True
            rec.flush_reason = "fetch_dropped"
            # Dropped on the cycle it was fetched.
            rec.flush_cycle = cycle
        self.next_id += 1
        return rec

    def on_branch_resolved_edge(self, cycle, sample):
        """Bind a resolution pulse (branch_unit.sv:66) to the oldest
        unresolved in-flight CTRL_FLOW record issued before it with its PC. A
        loop iteration resolved but not yet committed must not take the next
        iteration's resolution."""
        if sample.get("resolved_branch_i.valid") != "1":
            return
        pc = binary_to_int(sample.get("resolved_branch_i.pc"))
        candidates = [
            rec for rec in self.issued.values()
            if rec.fu == "CTRL_FLOW" and rec.bp_resolve_cycle is None
            and rec.is_cycle < cycle and pc is not None
            and pc_value(rec) == pc]
        if not candidates:
            self.n_unmatched_resolutions += 1
            return
        rec = min(candidates, key=lambda candidate: candidate.is_cycle)
        rec.bp_resolve_cycle = cycle
        rec.bp_resolved_cf = cf_name(
            binary_to_int(sample.get("resolved_branch_i.cf_type")))
        rec.bp_resolved_target = binary_to_int(
            sample.get("resolved_branch_i.target_address"))
        rec.bp_resolved_taken = (
            sample.get("resolved_branch_i.is_taken") == "1")
        rec.bp_mispredict = (
            sample.get("resolved_branch_i.is_mispredict") == "1")

    def flush_record(self, rec, reason, cycle):
        rec.flushed = True
        rec.flush_reason = reason
        rec.flush_cycle = cycle
        self.completed.append(rec)

    def flush_fetched(self, reason, cycle):
        while self.fetched:
            self.flush_record(self.fetched.popleft(), reason, cycle)
            if reason == "eof":
                self.n_drained_fetched += 1
            else:
                self.n_flushed_fetched += 1

    def flush_issued(self, reason, cycle):
        self.flushed_tids = set(self.issued)
        self.flushed_tids_cycle = cycle
        for rec in self.issued.values():
            self.flush_record(rec, reason, cycle)
            if reason == "eof":
                self.n_drained_issued += 1
            else:
                self.n_flushed_issued += 1
        self.issued.clear()

    def finalise(self):
        # Still in flight when the VCD ended, which the core never flushed,
        # so reason eof and no flush_cycle, since a cycle would invent one.
        self.flush_fetched("eof", None)
        self.flush_issued("eof", None)
        self.completed.sort(key=lambda rec: rec.id)


class LsuFsmTracker:
    """One LSU FSM, load_unit or store_unit, attributing its admissions and
    releases to in-flight records through lsu_ctrl.trans_id. An admission is
    IDLE to busy, a pop in admit_state, or admit_state to another busy
    state, the three ways a request enters (the IDLE and SEND_TAG cases at
    load_unit.sv:254 and :320, IDLE and VALID_STORE at store_unit.sv:160 and
    :179)."""

    def __init__(self, names, admit_state):
        self.names = names
        self.admit_state = admit_state
        self.active = None
        self.last_state = None
        self.pending_tid = None

    def admit(self, cycle, rec, state):
        self.active = rec
        if rec is not None:
            rec.lsu_admit_cycle = cycle
            self.log_state(rec, cycle, state)

    def release(self, cycle):
        if self.active is not None:
            self.active.lsu_release_cycle = cycle - 1

    @staticmethod
    def log_state(rec, cycle, state):
        if rec.lsu_state_history is None:
            rec.lsu_state_history = []
        rec.lsu_state_history.append({"cycle": cycle, "state": state})

    def on_edge(self, cycle, state_str, last_ctrl_tid, ctrl_tid, pop_str,
                issued):
        if state_str is None:
            return
        state = self.names.get(binary_to_int(state_str), f"?{state_str}")
        if self.last_state is None:
            self.last_state = state_str
            return
        if self.pending_tid is not None:
            # pop_ld_i or pop_st_i on the last edge took a request while busy,
            # and the FSM hands over to it now.
            self.release(cycle)
            self.admit(cycle, issued.get(binary_to_int(self.pending_tid)),
                       state)
            self.pending_tid = None
        elif state_str != self.last_state:
            last = self.names.get(binary_to_int(self.last_state), "?")
            admitted = issued.get(binary_to_int(last_ctrl_tid))
            if last == "IDLE" and state != "IDLE":
                self.admit(cycle, admitted, state)
            elif last == self.admit_state and state != "IDLE":
                self.release(cycle)
                self.admit(cycle, admitted, state)
            elif state == "IDLE":
                self.release(cycle)
                self.active = None
            elif self.active is not None:
                self.log_state(self.active, cycle, state)
        self.last_state = state_str
        if (pop_str == "1" and state == self.admit_state
                and ctrl_tid is not None):
            self.pending_tid = ctrl_tid


class DcacheEventLog:
    """The HPDcache miss handler's allocation, check and refill-response
    pulses in cycle order, and the cycles its refill FSM was busy."""

    def __init__(self):
        self.events = []
        self.refill_active_cycles = []

    def on_edge(self, cycle, sample):
        if sample.get("mshr_alloc_i") == "1":
            prefetch = binary_to_int(sample.get("mshr_alloc_is_prefetch_i"))
            self.events.append({
                "cycle": cycle, "type": "alloc",
                "sid": binary_to_int(sample.get("mshr_alloc_sid_i")),
                "tid": binary_to_int(sample.get("mshr_alloc_tid_i")),
                "is_prefetch": None if prefetch is None else prefetch == 1,
                "nline": binary_to_int(sample.get("mshr_alloc_nline_i")),
            })
        # The check path carries no requester id, so a check is attributed
        # to a record by its cycle alone.
        if sample.get("mshr_check_i") == "1":
            hit = sample.get("mshr_check_hit_o") == "1"
            self.events.append({
                "cycle": cycle, "type": "check_hit" if hit else "check_miss",
                "nline": binary_to_int(sample.get("mshr_check_nline_i")),
            })
        if sample.get("refill_core_rsp_valid_o") == "1":
            self.events.append({
                "cycle": cycle, "type": "refill_rsp",
                "tid": binary_to_int(sample.get("refill_core_rsp_o.tid")),
            })
        refill = binary_to_int(sample.get("refill_fsm_q"))
        if refill is not None and refill != REFILL_FSM_IDLE:
            self.refill_active_cycles.append(cycle)


class MemWritebackLog:
    """Dirty-line writeback handshakes in cycle order: flush_alloc hands a
    line over, the flush channel's request writes it and its response
    acknowledges it. finalise_mem_writebacks pairs them after the walk."""

    def __init__(self):
        self.allocs = []
        self.sends = []
        self.acks = []
        self.mshr_allocs = []

    def on_mem_wb_sample(self, cycle, sample):
        if (sample.get("flush_alloc") == "1"
                and sample.get("flush_alloc_ready") == "1"):
            producer = ("evict" if sample.get("ctrl_flush_alloc") == "1"
                        else "cmo")
            self.allocs.append((
                cycle, binary_to_hex(sample.get("flush_alloc_nline")),
                binary_to_int(sample.get("flush_alloc_way")), producer))
        if (sample.get("mem_req_write_flush_valid") == "1"
                and sample.get("mem_req_write_flush_ready") == "1"):
            self.sends.append((
                cycle,
                binary_to_int(sample.get("mem_req_write_flush.mem_req_id")),
                binary_to_hex(
                    sample.get("mem_req_write_flush.mem_req_addr"))))
        if (sample.get("mem_resp_write_flush_valid") == "1"
                and sample.get("mem_resp_write_flush_ready") == "1"):
            self.acks.append((
                cycle,
                binary_to_int(
                    sample.get("mem_resp_write_flush.mem_resp_w_id")),
                binary_to_hex(sample.get("flush_ack_nline"))))
        # Candidates for the eviction join, not evictions: the wback bit is
        # the incoming line's policy, not a dirty victim
        # (hpdcache_ctrl_pe.sv:582).
        if (sample.get("mshr_alloc_i") == "1"
                and sample.get("mshr_alloc_wback_i") == "1"):
            self.mshr_allocs.append((
                cycle, binary_to_hex(sample.get("mshr_alloc_nline_i")),
                binary_to_int(sample.get("mshr_alloc_victim_way_i"))))


class Mechanisms:
    """Every per-edge mechanism of one walk."""

    def __init__(self, resolved, nr_sb_entries, nr_commit_ports,
                 nr_wb_ports):
        self.resolved = resolved
        self.perf = PerfCounterSamples(resolved["icache"])
        self.pipeline = PipelineTracker(nr_sb_entries, nr_commit_ports,
                                        nr_wb_ports, resolved["forwarding"])
        self.icache = ICacheTimeline()
        self.load_fsm = LsuFsmTracker(LOAD_FSM_NAMES, LOAD_ADMIT_WHILE_BUSY)
        self.store_fsm = LsuFsmTracker(STORE_FSM_NAMES,
                                       STORE_ADMIT_WHILE_BUSY)
        self.last_lsu_ctrl_tid = None
        self.dcache = DcacheEventLog()
        self.mem_wb = MemWritebackLog()
        # Filled after the walk, by the passes of sections 8 and 9.
        self.n_unknown_bit_reads = 0
        self.mem_wb_stats = None
        self.bubble_counts = None

    def on_rising_edge(self, cycle, sample):
        """One rising edge. Commit runs before issue, so a slot released on
        this edge can be claimed on it, and writeback before issue, so a
        same-cycle forward sees the bus. The flush runs before writeback and
        issue, and the branch resolution runs last, once any flush on this
        edge has removed its record."""
        self.perf.on_edge(cycle, sample)
        pipeline = self.pipeline
        pipeline.on_commit_edge(cycle, sample)
        pipeline.on_flush_edge(cycle, sample)
        pipeline.on_writeback_edge(cycle, sample)
        pipeline.on_issue_edge(cycle, sample)
        pipeline.on_fetch_edge(cycle, sample)
        if self.resolved["icache"]:
            self.icache.on_edge(cycle, sample)
        if self.resolved["lsu_fsm"]:
            self.on_lsu_edge(cycle, sample)
        if self.resolved["dcache"]:
            self.dcache.on_edge(cycle, sample)
        if self.resolved["mem_writeback"]:
            self.mem_wb.on_mem_wb_sample(cycle, sample)
        if self.resolved["bp_resolution"]:
            pipeline.on_branch_resolved_edge(cycle, sample)

    def on_lsu_edge(self, cycle, sample):
        ctrl_tid = sample.get("lsu_ctrl.trans_id")
        issued = self.pipeline.issued
        self.load_fsm.on_edge(
            cycle, sample.get("load_unit.state_q"), self.last_lsu_ctrl_tid,
            ctrl_tid, sample.get("lsu_bypass.pop_ld_i"), issued)
        self.store_fsm.on_edge(
            cycle, sample.get("store_unit.state_q"), self.last_lsu_ctrl_tid,
            ctrl_tid, sample.get("lsu_bypass.pop_st_i"), issued)
        if ctrl_tid is not None:
            self.last_lsu_ctrl_tid = ctrl_tid


def stream_and_extract(f, mechanisms, sample, run):
    """Walk the VCD body once. A timestamp line closes the changes of the
    timestamp before it, and a clock that went from 0 to 1 across them was a
    rising edge, whose values are the post-edge ones Verilator dumps."""
    state = sample.state
    clk = sample.ids["clk_i"]
    tracked = set(sample.ids.values())
    progress = run.progress
    completed = mechanisms.pipeline.completed
    raw = f.buffer
    cycle = -1
    n_lines = n_changes = last_ts = 0
    first_edge_ts = vcd_clock_period = None
    first_ts_seen = False
    ends_cleanly = True
    clk_at_ts_start = "0"
    for line in f:
        n_lines += 1
        if not n_lines & PROGRESS_LINE_MASK:
            progress.update(n_lines, len(completed), raw.tell())
        # The only mark a cut leaves, since the header, the period and the
        # cycle-to-timestamp relation all stay consistent across one.
        ends_cleanly = line.endswith("\n")
        line = line.rstrip()
        if not line:
            continue
        first = line[0]
        if first == "#":
            if first_ts_seen:
                if clk_at_ts_start == "0" and state.get(clk) == "1":
                    # last_ts is the timestamp of this rising edge.
                    cycle += 1
                    if first_edge_ts is None:
                        first_edge_ts = last_ts
                    elif vcd_clock_period is None:
                        vcd_clock_period = last_ts - first_edge_ts
                    mechanisms.on_rising_edge(cycle, sample)
            else:
                first_ts_seen = True
            try:
                last_ts = int(line[1:])
            except ValueError:
                pass
            clk_at_ts_start = state.get(clk) or "0"
            continue
        if first in "01xXzZ":
            value, vcd_id = first, line[1:]
        elif first in "bBrR":
            space = line.find(" ")
            if space <= 0:
                continue
            value, vcd_id = line[1:space], line[space + 1:]
        else:
            continue
        n_changes += 1
        if vcd_id in tracked:
            state[vcd_id] = value
    # The last timestamp's changes have no timestamp line after them.
    if first_ts_seen and clk_at_ts_start == "0" and state.get(clk) == "1":
        cycle += 1
        mechanisms.on_rising_edge(cycle, sample)
    mechanisms.pipeline.finalise()
    run.n_lines, run.n_changes, run.last_ts = n_lines, n_changes, last_ts
    run.n_cycles = cycle + 1
    run.first_edge_ts = first_edge_ts
    run.vcd_clock_period = vcd_clock_period
    run.ends_cleanly = ends_cleanly


# ============================================================================
# 8. Record assembly
# ============================================================================

def index_events(events):
    """Deliveries per fetch-block address, sorted by fe2_cycle, with those
    cycles beside them for binary search, since a hot loop puts a delivery
    per iteration on one block."""
    by_word = defaultdict(list)
    for event in events:
        by_word[event.vaddr_word].append(event)
    fe2_keys = {}
    for word, word_events in by_word.items():
        word_events.sort(key=lambda event: event.fe2_cycle)
        fe2_keys[word] = [event.fe2_cycle for event in word_events]
    return by_word, fe2_keys


def find_event(index, word, floor, top, latest):
    """The latest delivery for word with fe2_cycle at most top, or the
    earliest when latest is false, and with fe2_cycle and fe1_cycle at least
    floor when floor is not None."""
    by_word, fe2_keys = index
    events = by_word.get(word)
    if not events:
        return None
    keys = fe2_keys[word]
    stop = bisect.bisect_right(keys, top)
    start = 0 if floor is None else bisect.bisect_left(keys, floor)
    order = range(stop - 1, start - 1, -1) if latest else range(start, stop)
    for i in order:
        if floor is None or events[i].fe1_cycle >= floor:
            return events[i]
    return None


def last_fetch_cycle(rec):
    """The delivery of a record's last fetch, the high one on a wrap, since a
    redirect cannot come before it and counting from the request would take
    in the record's own I-cache stall."""
    if rec.wraps_line and rec.fe2_hi_cycle is not None:
        return rec.fe2_hi_cycle
    return rec.fe2_lo_cycle


def last_request_cycle(rec):
    """The request of a record's last fetch, the high one on a wrap."""
    if rec.wraps_line and rec.fe1_hi_cycle is not None:
        return rec.fe1_hi_cycle
    return rec.fe1_lo_cycle


def set_low_fetch(rec, event):
    rec.fe1_lo_cycle = event.fe1_cycle
    rec.fe2_lo_cycle = event.fe2_cycle
    rec.ic_miss_lo = event.is_miss


def set_high_fetch(rec, event):
    rec.fe1_hi_cycle = event.fe1_cycle
    rec.fe2_hi_cycle = event.fe2_cycle
    rec.ic_miss_hi = event.is_miss


def match_records_to_events(records, index):
    """Bind each record's low fetch to the latest delivery for its block at
    or before fe_out_cycle, and a wrapping record's high fetch to the latest
    delivery for the next block no older than the low request. A flushed
    record killed before the I-cache answered gets synthesised cycles, others
    stay null. Returns the number of high fetches bound."""
    n_high_bound = 0
    for rec in records:
        pc = pc_value(rec)
        if pc is None:
            continue
        low = find_event(index, pc & ~FETCH_OFFSET_MASK, None,
                         rec.fe_out_cycle, latest=True)
        if low is not None:
            set_low_fetch(rec, low)
        elif rec.flushed:
            rec.fe1_lo_cycle = max(0, rec.fe_out_cycle - FETCH_SYNTH_DEPTH)
            rec.fe2_lo_cycle = max(0, rec.fe_out_cycle - FETCH_SYNTH_DEPTH
                                   + 1)
            rec.synthesised_stages = ["fe1", "fe2"]
            rec.ic_miss_lo = rec.ic_miss_hi = None
        if rec.wraps_line:
            # Without the floor a stale prefetch of the next block would win,
            # the front end not blocking on the high fetch.
            floor = rec.fe1_lo_cycle if rec.fe1_lo_cycle is not None else 0
            high = find_event(index, (pc + 2) & ~FETCH_OFFSET_MASK, floor,
                              rec.fe_out_cycle, latest=True)
            if high is not None:
                set_high_fetch(rec, high)
                n_high_bound += 1
    return n_high_bound


def repair_fetch_order(records, index):
    """Keep fe1_lo_cycle monotonic in fetch order. The binding can give a
    loop iteration the previous iteration's delivery, so such a record is
    rebound to a later delivery for its block, or, when the line stayed
    cached and the I-cache never answered again, synthesised off the record
    before it. Returns (rebound, synthesised)."""
    last_fe1 = -1
    last = None
    n_rebound = n_synthesised = 0
    for rec in records:
        if rec.fe1_lo_cycle is None:
            continue
        if rec.fe1_lo_cycle >= last_fe1:
            last_fe1, last = rec.fe1_lo_cycle, rec
            continue
        pc = pc_value(rec)
        if pc is None:
            continue
        block = pc & ~FETCH_OFFSET_MASK
        low = find_event(index, block, last_fe1, rec.fe_out_cycle,
                         latest=False)
        if low is not None:
            set_low_fetch(rec, low)
            # Measured again, so nothing on it is synthesised any more.
            rec.synthesised_stages = []
            n_rebound += 1
            if rec.wraps_line:
                high = find_event(index, (pc + 2) & ~FETCH_OFFSET_MASK,
                                  low.fe1_cycle, rec.fe_out_cycle,
                                  latest=False)
                if high is not None:
                    set_high_fetch(rec, high)
                else:
                    # The old high fetch predates the new low one, which a
                    # wrap cannot do, so it is no longer observed.
                    rec.fe1_hi_cycle = rec.fe2_hi_cycle = None
                    rec.ic_miss_hi = None
        else:
            synthesise_fetch(rec, last, last_fe1, block)
            n_synthesised += 1
        last_fe1, last = rec.fe1_lo_cycle, rec
    return n_rebound, n_synthesised


def synthesise_fetch(rec, last, last_fe1, block):
    """Sequential fetch cycles for a record no delivery fits: the last
    record's fetch when they share a block, one cycle after its last fetch
    otherwise, and never closer to fe_out_cycle than the front-end depth
    unless that would break fetch order."""
    depth = FETCH_SYNTH_DEPTH_WRAP if rec.wraps_line else FETCH_SYNTH_DEPTH
    ceiling = rec.fe_out_cycle - depth
    fe1 = None
    last_pc = pc_value(last) if last is not None else None
    if last_pc is not None:
        last_block = last_pc & ~FETCH_OFFSET_MASK
        if last_block == block:
            fe1 = last.fe1_lo_cycle
        elif (last.wraps_line and last_block + FETCH_BYTES == block
              and last.fe1_hi_cycle is not None):
            fe1 = last.fe1_hi_cycle
        elif last.wraps_line and last.fe1_hi_cycle is not None:
            fe1 = last.fe1_hi_cycle + 1
        else:
            fe1 = last.fe1_lo_cycle + 1
    if fe1 is None:
        # With no record before it, anchoring on fe_out_cycle alone would
        # overstate the gap when issue stalled and the fetch sat queued.
        fe1 = max(last_fe1, ceiling)
    fe1 = max(fe1, last_fe1)
    if fe1 > ceiling:
        fe1 = max(last_fe1, ceiling)
    rec.fe1_lo_cycle = fe1
    rec.fe2_lo_cycle = fe1 + 1
    if rec.wraps_line:
        # The high request shares the low delivery's cycle, pipelined.
        rec.fe1_hi_cycle = rec.fe2_lo_cycle
        rec.fe2_hi_cycle = rec.fe1_hi_cycle + 1
    rec.synthesised_stages = ["fe1", "fe2"]
    rec.ic_miss_lo = rec.ic_miss_hi = None


def pair_rvc_fetches(records):
    """Two compressed instructions in one fetch block arrive in one delivery,
    so the second takes the first's fetch cycles, which the binding of each
    record alone can pick apart. Returns the number of records changed."""
    n_paired = 0
    for last, rec in zip(records, records[1:]):
        if (not (last.is_compressed and rec.is_compressed)
                or last.fe1_lo_cycle is None
                or rec.fe1_lo_cycle == last.fe1_lo_cycle):
            continue
        last_pc, pc = pc_value(last), pc_value(rec)
        if (last_pc is None or pc is None or pc != last_pc + 2
                or (pc & ~FETCH_OFFSET_MASK)
                != (last_pc & ~FETCH_OFFSET_MASK)):
            continue
        rec.fe1_lo_cycle = last.fe1_lo_cycle
        rec.fe2_lo_cycle = last.fe2_lo_cycle
        rec.ic_miss_lo = last.ic_miss_lo
        # The timing is the first record's wholesale, so its provenance is.
        rec.synthesised_stages = list(last.synthesised_stages)
        n_paired += 1
    return n_paired


def attribute_dc_events(records, dc_log):
    """Bind D-cache events to LOAD and STORE records by access window, from
    admission to release plus the lookahead to the allocation. Each LSU FSM
    serves one request at a time, so an event in a window belongs to that
    access, and the first window to claim an event keeps it, which needs the
    records in id order."""
    events = dc_log.events
    event_cycles = [event["cycle"] for event in events]
    refill = dc_log.refill_active_cycles
    claimed = {"LOAD": set(), "STORE": set(), "check_hit": set()}
    miss_sid = {"LOAD": LOAD_UNIT_SID, "STORE": STORE_PORT_SID}
    lookahead = {"LOAD": HPDCACHE_LOAD_LOOKAHEAD,
                 "STORE": HPDCACHE_STORE_LOOKAHEAD}
    for rec in records:
        if rec.fu not in miss_sid:
            continue
        admit, release = rec.lsu_admit_cycle, rec.lsu_release_cycle
        # Without an access window nothing was observed, so the verdicts stay
        # null rather than read as a hit.
        if admit is None or release is None:
            continue
        rec.dc_miss = rec.dc_coalesced = False
        window_end = release + lookahead[rec.fu]
        in_window = []
        for i in range(bisect.bisect_left(event_cycles, admit), len(events)):
            event = events[i]
            if event["cycle"] > window_end:
                break
            in_window.append(event)
            if (event["type"] == "alloc" and event["sid"] == miss_sid[rec.fu]
                    and i not in claimed[rec.fu]):
                claimed[rec.fu].add(i)
                rec.dc_miss = True
            # Stores raise no check, so only a load coalesces.
            elif (event["type"] == "check_hit" and rec.fu == "LOAD"
                  and i not in claimed["check_hit"]):
                claimed["check_hit"].add(i)
                rec.dc_coalesced = True
        rec.dc_event_log = in_window
        # Measured on loads only, so a store's stays null.
        if rec.fu == "LOAD":
            j = bisect.bisect_left(refill, admit)
            rec.dc_refill_overlap = j < len(refill) and refill[j] <= release


def pair_flush_channel(log):
    """One writeback per flush-channel response, paired with its request by
    slot for the AXI write time. Returns (writebacks, non-negative times,
    responses without a request, requests without a response, negative
    times)."""
    sends_by_slot = defaultdict(deque)
    for cycle, slot, addr in log.sends:
        sends_by_slot[slot].append((cycle, addr))
    writebacks, latencies = [], []
    n_acks_without_send = n_negative = 0
    for ack_cycle, slot, nline in log.acks:
        if not sends_by_slot.get(slot):
            n_acks_without_send += 1
            continue
        send_cycle, addr = sends_by_slot[slot].popleft()
        n_axi = ack_cycle - send_cycle
        # A mispaired slot gives a negative time, counted apart so it cannot
        # drag the aggregate.
        if n_axi < 0:
            n_negative += 1
        else:
            latencies.append(n_axi)
        writebacks.append({
            "alloc_cycle": None, "send_cycle": send_cycle,
            "ack_cycle": ack_cycle, "evict_cycle": None, "flush_slot": slot,
            "way": None, "nline": nline, "addr": addr,
            "evict_incoming_nline": None, "producer": None,
            "is_linked": False, "n_axi_write_cycles": n_axi,
            "n_residency_cycles": None,
        })
    n_sends_without_ack = sum(len(queue) for queue in sends_by_slot.values())
    return (writebacks, latencies, n_acks_without_send, n_sends_without_ack,
            n_negative)


def join_allocations(writebacks, allocs):
    """Give each writeback the flush_alloc of its line, first in first out
    per nline, for the residency, the victim way and the producer."""
    allocs_by_nline = defaultdict(deque)
    for cycle, nline, way, producer in allocs:
        allocs_by_nline[nline].append((cycle, way, producer))
    for wb in writebacks:
        queue = allocs_by_nline.get(wb["nline"])
        if queue:
            cycle, way, producer = queue.popleft()
            wb["alloc_cycle"] = cycle
            wb["n_residency_cycles"] = wb["ack_cycle"] - cycle
            wb["way"] = onehot_to_index(way)
            wb["producer"] = producer


def link_evictions(writebacks, mshr_allocs, dc_sets):
    """Link each writeback to the miss allocation that evicted its line, by
    (set, victim way) and nearest in time. Returns (evictions, CMO flushes,
    linked)."""
    set_mask = dc_sets - 1 if dc_sets else 0
    candidates = defaultdict(list)
    for cycle, nline, victim_way in mshr_allocs:
        line_set = None if nline is None else int(nline, 16) & set_mask
        candidates[(line_set, victim_way)].append((cycle, nline))
    for joinable in candidates.values():
        joinable.sort()
    used = defaultdict(set)
    n_evict = n_cmo = n_linked = 0
    for wb in writebacks:
        if wb["producer"] == "cmo":
            # A CMO flush writes dirty lines back with no miss, so there is
            # no eviction to join.
            n_cmo += 1
            continue
        if wb["producer"] == "evict":
            n_evict += 1
        line_set = (None if wb["nline"] is None
                    else int(wb["nline"], 16) & set_mask)
        key = (line_set, None if wb["way"] is None else 1 << wb["way"])
        anchor = (wb["alloc_cycle"] if wb["alloc_cycle"] is not None
                  else wb["send_cycle"])
        best = None
        for i, (cycle, incoming) in enumerate(candidates.get(key, ())):
            if i in used[key] or abs(cycle - anchor) > EVICTION_JOIN_WINDOW:
                continue
            if best is None or abs(cycle - anchor) < abs(best[1] - anchor):
                best = (i, cycle, incoming)
        if best is not None:
            used[key].add(best[0])
            wb["evict_cycle"], wb["evict_incoming_nline"] = best[1], best[2]
            wb["is_linked"] = True
            n_linked += 1
    return n_evict, n_cmo, n_linked


def finalise_mem_writebacks(log, dc_sets):
    """mem_writebacks in send order, and the mem_writeback statistics."""
    (writebacks, latencies, n_acks_without_send, n_sends_without_ack,
     n_negative) = pair_flush_channel(log)
    join_allocations(writebacks, log.allocs)
    writebacks.sort(key=lambda wb: wb["send_cycle"])
    n_evict, n_cmo, n_linked = link_evictions(writebacks, log.mshr_allocs,
                                              dc_sets)
    ordered = sorted(latencies)
    stats = {
        "n_allocs": len(log.allocs),
        "n_sends": len(log.sends),
        "n_acks": len(log.acks),
        "n_matched_pairs": len(latencies),
        "n_acks_without_send": n_acks_without_send,
        "n_sends_without_ack": n_sends_without_ack,
        "n_mshr_allocs_sampled": len(log.mshr_allocs),
        "n_allocs_evict": n_evict,
        "n_allocs_cmo": n_cmo,
        "n_linked": n_linked,
        "n_unlinked": len(writebacks) - n_cmo - n_linked,
        "n_negative_latency": n_negative,
        "axi_write": {
            "n_samples": len(ordered),
            "min_cycles": ordered[0] if ordered else None,
            # The lower middle of an even count, a time some writeback took.
            "median_cycles": (statistics.median_low(ordered) if ordered
                              else None),
            "max_cycles": ordered[-1] if ordered else None,
            "histogram": {str(k): v
                          for k, v in sorted(Counter(latencies).items())},
        },
    }
    return writebacks, stats


def parse_disasm_list(path):
    """An objdump listing as a map from PC to its text, the mnemonic,
    operands and objdump's symbol comment with whitespace collapsed so no tab
    reaches the page. Lines that are not instructions are skipped."""
    disasm = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            match = DISASM_LINE.match(line.rstrip("\n"))
            if match:
                disasm[int(match.group(1), 16)] = " ".join(
                    match.group(3).split())
    return disasm


def apply_disasm(records, disasm):
    """Fill each record's disasm by PC. Returns (annotated, without a PC,
    unmapped), unmapped being PCs outside the listing such as the bootrom."""
    n_annotated = n_no_pc = n_unmapped = 0
    for rec in records:
        pc = pc_value(rec)
        if pc is None:
            n_no_pc += 1
        elif pc in disasm:
            rec.disasm = disasm[pc]
            n_annotated += 1
        else:
            n_unmapped += 1
    return n_annotated, n_no_pc, n_unmapped


# ============================================================================
# 9. Whole-run derived fields
# ============================================================================

def forwarding_producers(records):
    """Turn each forwarded operand's scoreboard slot into the record that
    produced it: the latest record with that trans_id written back at or
    before the consumer's issue cycle, since slots are reused. Returns the
    operands left without a producer."""
    by_tid = defaultdict(list)
    for rec in records:
        if rec.trans_id is not None and rec.wb_cycle is not None:
            by_tid[rec.trans_id].append(rec)
    wb_keys = {}
    for tid, producers in by_tid.items():
        producers.sort(key=lambda rec: rec.wb_cycle)
        wb_keys[tid] = [rec.wb_cycle for rec in producers]
    n_unresolved = 0
    for rec in records:
        for n in (1, 2, 3):
            if not getattr(rec, f"fwd_rs{n}_used"):
                continue
            tid = getattr(rec, f"fwd_rs{n}_tid")
            i = bisect.bisect_right(wb_keys.get(tid, ()), rec.is_cycle) - 1
            if i < 0:
                n_unresolved += 1
            else:
                setattr(rec, f"fwd_rs{n}_producer_id", by_tid[tid][i].id)
    return n_unresolved


def branch_outcomes(records):
    """bp_outcome and bp_decided_at of every resolved, committed CTRL_FLOW
    record. A mispredict with no prediction made is unpred."""
    for rec in records:
        if (rec.fu != "CTRL_FLOW" or rec.flushed
                or rec.bp_mispredict is None):
            continue
        if not rec.bp_mispredict:
            rec.bp_outcome, rec.bp_decided_at = "correct", "fe2"
        elif rec.bp_predicted_cf in (None, "NoCF"):
            rec.bp_outcome, rec.bp_decided_at = "unpred", "ex"
        else:
            rec.bp_outcome, rec.bp_decided_at = "mispred", "ex"


def mark_bubble(causer, recovery, kind, n_flushed, n_cycles, n_gap):
    causer.caused_bubble_kind = kind
    causer.caused_bubble_recovery_id = recovery.id
    causer.n_caused_bubble_cycles = n_cycles
    causer.n_caused_bubble_flushed = n_flushed
    recovery.bubble_kind = kind
    recovery.bubble_causer_id = causer.id
    recovery.n_bubble_cycles = n_cycles
    recovery.n_bubble_gap_cycles = n_gap


def bubble_gap(causer, recovery):
    """Idle cycles between the causer's last delivery and the recovery's
    first request, exclusive at both ends, None when either is missing."""
    end = last_fetch_cycle(causer)
    if end is None or recovery.fe1_lo_cycle is None:
        return None
    return max(0, recovery.fe1_lo_cycle - end - 1)


def flush_causer(ordered, i, j):
    """The causer of a flush bubble between ordered[i] and the recovery
    ordered[j], and the flushed records it leaves between them."""
    causer = ordered[i]
    n_flushed = j - i - 1
    # A committing CSR write flushes itself, leaving a wb_cycle and no
    # co_cycle, so the first such record in the run is the cause.
    if causer.fu != "CSR":
        for k in range(i + 1, j):
            candidate = ordered[k]
            if (candidate.fu == "CSR" and candidate.flushed
                    and candidate.wb_cycle is not None):
                return candidate, n_flushed - 1
    return causer, n_flushed


def tag_bubbles(records):
    """Tag every bubble on its causer and its recovery. The first pass finds
    [not flushed][flushed run][not flushed] in id order, the second a gap
    after taken control flow with nothing flushed, and the recovery's bubble
    is then copied to the records sharing its fetch. Returns the causer
    counts by kind."""
    counts = {"pred_taken": 0, "unpred": 0, "mispred": 0, "flush": 0}
    ordered = sorted(records, key=lambda rec: rec.id)
    n = len(ordered)
    i = 0
    while i < n:
        while i < n and ordered[i].flushed:
            i += 1
        j = i + 1
        while j < n and ordered[j].flushed:
            j += 1
        if j >= n:
            break
        if j == i + 1:
            i = j
            continue
        causer, recovery = ordered[i], ordered[j]
        n_flushed = j - i - 1
        # The rule branch_outcomes uses, so bp_outcome and the bubble agree.
        if causer.fu == "CTRL_FLOW" and causer.bp_mispredict:
            kind = ("unpred" if causer.bp_predicted_cf in (None, "NoCF")
                    else "mispred")
        else:
            kind = "flush"
            causer, n_flushed = flush_causer(ordered, i, j)
        gap = bubble_gap(causer, recovery)
        # A bubble whose length cannot be measured is not tagged, since a
        # null n_bubble_cycles means no bubble.
        if gap is not None:
            mark_bubble(causer, recovery, kind, n_flushed, gap, gap)
            counts[kind] += 1
        # The recovery may cause the next bubble in turn.
        i = j

    for i, causer in enumerate(ordered):
        # fu == CTRL_FLOW too, since only control flow redirects, and taken
        # means resolved taken, or a jump or return when no resolution was
        # bound to the record.
        if (causer.flushed or causer.fu != "CTRL_FLOW"
                or causer.caused_bubble_kind is not None
                or not (causer.bp_resolved_taken is True
                        or causer.bp_predicted_cf in ("Jump", "Return"))):
            continue
        end = last_fetch_cycle(causer)
        if causer.fe1_lo_cycle is None or end is None:
            continue
        recovery = None
        for k in range(i + 1, n):
            if not ordered[k].flushed and ordered[k].fe1_lo_cycle is not None:
                recovery = ordered[k]
                break
        if recovery is None or recovery.bubble_causer_id is not None:
            continue
        gap = recovery.fe1_lo_cycle - end - 1
        if gap < 1:
            continue
        if causer.bp_mispredict and causer.bp_predicted_cf in (None, "NoCF"):
            kind, n_cycles = "unpred", gap
        elif causer.bp_mispredict:
            kind, n_cycles = "mispred", gap
        else:
            # The predecoder redirects in FE2, so a correct taken prediction
            # costs one cycle and the rest is queue backpressure.
            kind, n_cycles = "pred_taken", min(gap, 1)
        mark_bubble(causer, recovery, kind, 0, n_cycles, gap)
        counts[kind] += 1

    for last, rec in zip(ordered, ordered[1:]):
        if (rec.bubble_causer_id is not None or rec.fe1_lo_cycle is None
                or rec.fe1_lo_cycle != last.fe1_lo_cycle
                or last.bubble_causer_id is None
                or not last.n_bubble_cycles):
            continue
        rec.bubble_kind = last.bubble_kind
        rec.bubble_causer_id = last.bubble_causer_id
        rec.n_bubble_cycles = last.n_bubble_cycles
        rec.n_bubble_gap_cycles = last.n_bubble_gap_cycles
        rec.bubble_shared_id = (last.bubble_shared_id
                                if last.bubble_shared_id is not None
                                else last.id)
    return counts


def pre_fetch_waits(records):
    """n_pre_fetch_wait_cycles and pre_fetch_wait_shared_id: cycles between
    the last non-flushed record's last request, the high one on a wrap, and
    this one's request, less one, a record sharing the request sharing the
    first one's wait, 0 on a flushed record. A wrap's own wait between its two
    requests is its high-line wait, so counting it here would count it
    twice."""
    last = None
    for rec in records:
        if rec.fe1_lo_cycle is None:
            if not rec.flushed:
                last = rec
            continue
        if rec.flushed:
            rec.n_pre_fetch_wait_cycles = 0
            continue
        if (last is not None and last.fe1_lo_cycle is not None
                and rec.fe1_lo_cycle == last.fe1_lo_cycle):
            rec.n_pre_fetch_wait_cycles = last.n_pre_fetch_wait_cycles or 0
            rec.pre_fetch_wait_shared_id = (
                last.pre_fetch_wait_shared_id
                if last.pre_fetch_wait_shared_id is not None else last.id)
        elif last is None or last.fe1_lo_cycle is None:
            rec.n_pre_fetch_wait_cycles = 0
        else:
            rec.n_pre_fetch_wait_cycles = max(
                0, rec.fe1_lo_cycle - last_request_cycle(last) - 1)
        last = rec


# ============================================================================
# 10. Degradation census
# ============================================================================

# SHARED BEGIN py-degraded

# Needs: sys, py-log

DEGRADED_NAME_WIDTH = 22


class DegradedCensus:
    """The mechanisms a run could not resolve. They travel in the JSON
    because stderr is gone once a long run has finished."""

    def __init__(self):
        self.entries = []

    def require(self, present, mechanism, effect):
        """Record mechanism as degraded unless present is true."""
        if not present:
            self.entries.append({"mechanism": mechanism, "effect": effect})


def print_degraded(degraded):
    """Printed last, so it is the final thing on screen after a long run."""
    if not degraded:
        log_info("All mechanisms resolved. metadata.degraded is empty.")
        return
    print(f"[DEGRADED] {len(degraded)} mechanism(s) did not resolve. The "
          f"affected fields are null or empty, not measured.",
          file=sys.stderr)
    for entry in degraded:
        print(f"           {entry['mechanism']:<{DEGRADED_NAME_WIDTH}} "
              f"{entry['effect']}", file=sys.stderr)
    print("           metadata.degraded in the JSON records the same list.",
          file=sys.stderr)


def exit_status(degraded, strict, input_noun):
    """3 under --strict when anything is degraded, else 0."""
    if degraded and strict:
        log_error(f"--strict was given and the {input_noun} is degraded, "
                  f"exiting with 3.")
        return 3
    return 0

# SHARED END py-degraded


# The mechanisms that may be absent, in the order metadata.degraded lists
# them, with the keys each one leaves null or empty.
MECHANISM_EFFECTS = {
    "icache": "fe1_lo_cycle, fe1_hi_cycle, fe2_lo_cycle and fe2_hi_cycle are "
              "null except on synthesised flushed records, ic_miss_lo and "
              "ic_miss_hi are null, and ic_events.miss_cycles and "
              "ic_events.deliveries are empty.",
    "lsu_fsm": "lsu_admit_cycle and lsu_release_cycle are null, and so are "
               "dc_miss, dc_coalesced and dc_refill_overlap, which need the "
               "access window.",
    "dcache": "dc_miss, dc_coalesced and dc_refill_overlap are null and "
              "dc_events.allocs is empty.",
    "mem_writeback": "mem_writebacks is empty.",
    "bp_resolution": "bp_resolved_cf, bp_resolved_target, bp_resolved_taken, "
                     "bp_mispredict, bp_resolve_cycle, bp_outcome and "
                     "bp_decided_at are null.",
    "forwarding": "fwd_rs1_used, fwd_rs2_used and fwd_rs3_used are false and "
                  "every fwd_rs*_via and fwd_rs*_producer_id is null.",
}


def build_degraded(resolved, run, pipeline):
    """metadata.degraded: every mechanism that did not resolve, then what
    the body of the VCD shows. The mechanisms are decided from the header,
    so a cut VCD degrades like a whole one, and the last six read the body."""
    census = DegradedCensus()
    for mechanism, effect in MECHANISM_EFFECTS.items():
        census.require(resolved[mechanism], mechanism, effect)
    census.require(
        run.ends_cleanly, "truncated",
        "The VCD's last line has no newline, so the file was cut mid-record: "
        "instructions stops early and every count in metadata.stats is a "
        "lower bound.")
    census.require(
        run.n_changes > 0, "no_value_changes",
        "The VCD holds a header and no value changes, so instructions and "
        "every event array are empty.")
    census.require(
        run.n_cycles >= 1, "no_rising_edges",
        "The clock never rose in the VCD, so instructions and every event "
        "array are empty.")
    census.require(
        bool(pipeline.completed), "no_records",
        "No instruction was recovered from the VCD, so instructions is "
        "empty.")
    census.require(
        pipeline.n_committed > 0, "no_commits",
        "No instruction committed, which a whole run of any program cannot "
        "produce, so every co_cycle is null and the VCD is most likely cut.")
    period = run.vcd_clock_period
    first = run.first_edge_ts or 0
    # A dump windowed with verilator_changes/custom_size_vcds starts part
    # way through the run, which moves every timestamp but not the period.
    census.require(
        not period or period <= 0 or first <= 2 * period, "windowed",
        f"The VCD's first rising edge is at timestamp {first:,}, so the dump "
        f"starts part way through the run: cycle 1 is its first edge, not "
        f"the core's, the instructions in flight there lack their earlier "
        f"stages, and metadata.stats covers the window only.")
    # A misidentified clock, where the edge count and the elapsed time
    # disagree. The margin is wide, so only the gross case trips.
    census.require(
        not period or period <= 0 or run.n_cycles <= 1
        or abs(run.last_ts - first - (run.n_cycles - 1) * period)
        <= 2 * period,
        "timestamp_implausible",
        f"The final timestamp {run.last_ts:,} does not match "
        f"{run.n_cycles:,} cycles of {period} VCD units from the first edge "
        f"at {first:,}, so the clock was misidentified and every cycle "
        f"number is suspect.")
    return census.entries


# ============================================================================
# 11. Metadata and writer
# ============================================================================

def build_stats(records, mechanisms, table_size, n_missing, bindings,
                disasm_counts):
    """metadata.stats, one dict in the order the JSON writes it."""
    pipeline = mechanisms.pipeline
    deliveries = mechanisms.icache.events
    n_rebound, n_synthesised, n_paired = bindings
    n_annotated, n_no_pc, n_unmapped = disasm_counts
    committed = [rec for rec in records if not rec.flushed]

    def count(predicate, among=records):
        return sum(1 for rec in among if predicate(rec))

    loads = [rec for rec in records if rec.fu == "LOAD"]
    stores = [rec for rec in records if rec.fu == "STORE"]
    allocs = [event for event in mechanisms.dcache.events
              if event["type"] == "alloc"]
    misses = [event for event in allocs if event["is_prefetch"] is not True]

    control = [rec for rec in records if rec.fu == "CTRL_FLOW"]
    resolved = [rec for rec in control if rec.bp_resolve_cycle is not None]
    predicted = Counter(rec.bp_predicted_cf for rec in control
                        if rec.bp_predicted_cf not in (None, "NoCF"))
    mispredicted = Counter(rec.bp_resolved_cf or "NoCF" for rec in resolved
                           if rec.bp_mispredict)
    n_mispredicts = sum(mispredicted.values())
    # Nothing resolved measures no rate, so it stays null.
    hit_rate = (round(100.0 * (len(resolved) - n_mispredicts)
                      / len(resolved), 2) if resolved else None)

    bubble_counts = mechanisms.bubble_counts
    via = Counter(getattr(rec, f"fwd_rs{n}_via") for rec in committed
                  for n in (1, 2, 3))
    return {
        "n_records": len(records),
        "n_committed": pipeline.n_committed,
        "n_flushed": (pipeline.n_flushed_fetched + pipeline.n_flushed_issued
                      + pipeline.n_drained_fetched
                      + pipeline.n_drained_issued),
        "n_flushed_fetched": pipeline.n_flushed_fetched,
        "n_flushed_issued": pipeline.n_flushed_issued,
        "n_drained_fetched": pipeline.n_drained_fetched,
        "n_drained_issued": pipeline.n_drained_issued,
        "n_unmatched_writebacks": pipeline.n_unmatched_writebacks,
        "n_discarded_writebacks": pipeline.n_discarded_writebacks,
        "n_unmatched_commits": pipeline.n_unmatched_commits,
        "n_unmatched_decodes": pipeline.n_unmatched_decodes,
        "n_unmatched_resolutions": pipeline.n_unmatched_resolutions,
        "n_signal_groups": table_size,
        "n_signal_groups_missing": n_missing,
        "n_unknown_bit_reads": mechanisms.n_unknown_bit_reads,
        "n_ic_deliveries": len(deliveries),
        "n_ic_delivery_hits": sum(1 for e in deliveries if not e.is_miss),
        "n_ic_delivery_misses": sum(1 for e in deliveries if e.is_miss),
        "n_ic_miss_pulses": len(mechanisms.perf.ic_miss_cycles),
        "n_ic_records_matched": count(lambda rec: rec.fe1_lo_cycle is not None
                                      and not rec.synthesised_stages),
        "n_ic_records_synthesised": count(
            lambda rec: bool(rec.synthesised_stages)),
        "n_ic_records_synthesised_monotonic": n_synthesised,
        "n_ic_records_rvc_paired": n_paired,
        "n_ic_records_rebound": n_rebound,
        "n_ic_records_unmatched": count(lambda rec: rec.fe1_lo_cycle is None),
        "n_disasm_annotated": n_annotated,
        "n_disasm_unmapped": n_unmapped,
        "n_disasm_no_pc": n_no_pc,
        "n_lsu_loads_tracked": count(lambda rec: bool(rec.lsu_state_history),
                                     loads),
        "n_lsu_stores_tracked": count(
            lambda rec: bool(rec.lsu_state_history), stores),
        "n_lsu_loads_untracked": count(
            lambda rec: not rec.lsu_state_history, loads),
        "n_lsu_stores_untracked": count(
            lambda rec: not rec.lsu_state_history, stores),
        "dc": {
            "n_events": len(mechanisms.dcache.events),
            "n_refill_active_cycles": len(
                mechanisms.dcache.refill_active_cycles),
            "n_loads": len(loads),
            "n_stores": len(stores),
            "n_miss_loads": count(lambda rec: rec.dc_miss is True, loads),
            "n_miss_stores": count(lambda rec: rec.dc_miss is True, stores),
            "n_coalesced_loads": count(lambda rec: rec.dc_coalesced is True,
                                       loads),
            "n_refill_overlap_loads": count(
                lambda rec: rec.dc_refill_overlap is True, loads),
            # evt_cache_read_miss_o counts every allocation but a prefetch
            # (hpdcache_ctrl_pe.sv:368), split here by requester.
            "n_miss_events": len(misses),
            "n_miss_events_loads": sum(1 for e in misses
                                       if e["sid"] == LOAD_UNIT_SID),
            "n_miss_events_stores": sum(1 for e in misses
                                        if e["sid"] == STORE_PORT_SID),
            "n_miss_events_other": sum(
                1 for e in misses
                if e["sid"] not in (LOAD_UNIT_SID, STORE_PORT_SID)),
        },
        "bp": {
            "n_control": len(control),
            "n_predictions": sum(predicted.values()),
            "n_resolutions": len(resolved),
            "n_mispredicts": n_mispredicts,
            "n_predictions_by_cf": {cf: predicted[cf] for cf in (
                "Branch", "Jump", "JumpR", "Return")},
            "n_mispredicts_by_cf": {cf: mispredicted[cf] for cf in (
                "Branch", "Jump", "JumpR", "Return", "NoCF")},
            "hit_rate_percent": hit_rate,
            "n_flushed_before_resolve": count(
                lambda rec: rec.flushed and rec.bp_resolve_cycle is None,
                control),
        },
        "bubbles": {
            "n_pred_taken": bubble_counts["pred_taken"],
            "n_unpred": bubble_counts["unpred"],
            "n_mispred": bubble_counts["mispred"],
            "n_flush": bubble_counts["flush"],
            "n_flushed": sum(rec.n_caused_bubble_flushed or 0
                             for rec in records),
            "n_cycles": sum(rec.n_caused_bubble_cycles or 0
                            for rec in records),
        },
        "mem_writeback": mechanisms.mem_wb_stats,
        "fwd": {
            "n_with_any_forward": count(
                lambda rec: rec.fwd_rs1_used or rec.fwd_rs2_used
                or rec.fwd_rs3_used, committed),
            "n_rs1_forwarded": count(lambda rec: rec.fwd_rs1_used, committed),
            "n_rs2_forwarded": count(lambda rec: rec.fwd_rs2_used, committed),
            "n_rs3_forwarded": count(lambda rec: rec.fwd_rs3_used, committed),
            "n_via_sb": via["sb"],
            "n_via_wb": via["wb"],
            "n_issue_cycles": pipeline.n_issue_cycles,
            "n_issue_cycles_with_any_wb": pipeline.n_issue_cycles_with_any_wb,
        },
    }


def build_config_params(nr_sb_entries, nr_commit_ports, nr_wb_ports,
                        dc_sets):
    """The build parameters read from the VCD's own declarations, the two
    the superscalar refusal fixes, and TRANS_ID_BITS derived from the slot
    count, so a sweep build reports its size."""
    params = {
        "NrCommitPorts": nr_commit_ports,
        "NrWbPorts": nr_wb_ports,
        "NrScoreboardEntries": nr_sb_entries,
        "TRANS_ID_BITS": (nr_sb_entries - 1).bit_length(),
        "FETCH_WIDTH": FETCH_WIDTH,
        "INSTR_PER_FETCH": INSTR_PER_FETCH,
    }
    if dc_sets is not None:
        params["DCACHE_NUM_SETS"] = dc_sets
    return params


def build_metadata(args, run, timescale, dc_sets, disasm_list_path,
                   degraded, stats):
    metadata = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "vcd_path": args.vcd,
        "vcd_scope_prefix": args.scope_prefix,
        "disasm_list_path": disasm_list_path,
        # A label, not a reading: no VCD names its build, so a sweep's VCDs
        # need --config-name or every JSON claims the default.
        "config_name": args.config_name,
        "clock_period": ASSUMED_CLOCK_PERIOD_PS,
        "time_unit": TIME_UNIT,
        "clock_period_source": "assumed",
        "vcd_clock_period": run.vcd_clock_period,
        "vcd_timescale": timescale,
        "dc_sets": dc_sets,
        "record_fields": record_fields(args.emit_diagnostics),
        "cycle_fields": list(CYCLE_FIELDS),
        "cycle_list_fields": list(CYCLE_LIST_FIELDS),
        "id_fields": list(ID_FIELDS),
        "id_list_fields": list(ID_LIST_FIELDS),
        "event_fields": EVENT_FIELDS,
        "event_twin_fields": EVENT_TWIN_FIELDS,
        "degraded": degraded,
        "clipped": None,
        "stats": stats,
    }
    if tuple(metadata) != METADATA_KEY_ORDER:
        raise ValueError("build_metadata drifted from METADATA_KEY_ORDER")
    return metadata


def event_objects(mechanisms):
    """ic_events and dc_events, in the order the JSON writes their keys."""
    perf = mechanisms.perf
    ic_events = {
        "access_cycles": perf.ic_access_cycles,
        "miss_cycles": perf.ic_miss_cycles,
        "deliveries": [
            {"fe1_cycle": event.fe1_cycle, "fe2_cycle": event.fe2_cycle,
             "is_miss": event.is_miss, "vaddr": f"0x{event.vaddr_word:x}"}
            for event in mechanisms.icache.events],
    }
    dc_events = {
        "access_cycles": perf.dc_access_cycles,
        "allocs": [
            {"cycle": event["cycle"], "sid": event["sid"],
             "is_prefetch": event["is_prefetch"]}
            for event in mechanisms.dcache.events
            if event["type"] == "alloc"],
    }
    return ic_events, dc_events


def record_fields(emit_diagnostics):
    """metadata.record_fields: the keys a record of this run can carry, in
    written order, the diagnostic lists only when they are written."""
    return [name for name in InstructionRecord.__dataclass_fields__
            if name not in INTERNAL_ONLY_FIELDS
            and (emit_diagnostics or name not in DIAGNOSTIC_ONLY_FIELDS)]


def record_dicts(records, emit_diagnostics):
    keys = record_fields(emit_diagnostics)
    return [{key: getattr(rec, key) for key in keys} for rec in records]


# SHARED BEGIN py-json-writer

# Needs: json, os

JSON_SEPARATORS = (",", ":")
FIELD_LIST_SUFFIXES = {
    "cycle_fields": "_cycle",
    "cycle_list_fields": "_cycles",
    "id_fields": "_id",
    "id_list_fields": "_ids",
}


def check_field_lists(records, metadata, core_names=()):
    """Raise unless metadata.record_fields names every key a record carries,
    in the order records write them, and the other field lists agree with
    it, since the pages and scripts trust the lists. A record may leave out
    any key, which reads as null. core_names are keys copied from a core
    whose suffix means nothing."""
    fields = metadata["record_fields"]
    position = {key: at for at, key in enumerate(fields)}
    if len(position) != len(fields):
        raise ValueError("metadata.record_fields names a key twice")
    for rec in records:
        last = -1
        for key in rec:
            if key not in position:
                raise ValueError(f"record {rec.get('id')} carries {key}, "
                                 f"missing from metadata.record_fields")
            if position[key] < last:
                raise ValueError(f"record {rec.get('id')} writes {key} out "
                                 f"of the order of metadata.record_fields")
            last = position[key]
    for list_name, suffix in FIELD_LIST_SUFFIXES.items():
        listed = metadata[list_name]
        stray = [key for key in listed
                 if key not in position or not key.endswith(suffix)]
        if stray:
            raise ValueError(f"metadata.{list_name} names {stray}, which "
                             f"record_fields lacks or which lack {suffix}")
        # n_ keys are counts, whatever their suffix says.
        unlisted = [key for key in fields if key.endswith(suffix)
                    and not key.startswith("n_") and key not in listed
                    and key not in core_names]
        if unlisted:
            raise ValueError(f"metadata.record_fields names {unlisted}, "
                             f"missing from metadata.{list_name}")
    events = metadata["event_fields"]
    orphans = [twin for twin, base in metadata["event_twin_fields"].items()
               if base not in events]
    if orphans:
        raise ValueError(f"metadata.event_twin_fields pairs {orphans} with "
                         f"an array event_fields does not name")


def row_paths(data):
    """The arrays of objects that leave out their null keys: the records,
    and every event array of objects. metadata keeps its nulls."""
    events = data["metadata"]["event_fields"]
    return {"instructions"} | {
        path for path, where in events.items()
        if isinstance(where, list) and where and isinstance(where[0], str)}


def fits_one_line(values):
    """Scalars, or short tuples of scalars such as spans and depth pairs."""
    return not any(isinstance(value, dict) or (
        isinstance(value, list)
        and any(isinstance(item, (dict, list)) for item in value))
        for value in values)


def _write_value(out, value, path, depth, rows):
    pad = "  " * depth
    if isinstance(value, dict) and value:
        out.write("{\n")
        last = len(value) - 1
        for i, (key, member) in enumerate(value.items()):
            child = f"{path}.{key}" if path else key
            out.write(f"{pad}  {json.dumps(key)}: ")
            _write_value(out, member, child, depth + 1, rows)
            out.write(",\n" if i < last else "\n")
        out.write(pad + "}")
    elif isinstance(value, list) and value and (
            path in rows or not fits_one_line(value)):
        out.write("[\n")
        last = len(value) - 1
        for i, element in enumerate(value):
            if path in rows:
                element = {key: item for key, item in element.items()
                           if item is not None}
            out.write(pad + "  ")
            out.write(json.dumps(element, separators=JSON_SEPARATORS))
            out.write(",\n" if i < last else "\n")
        out.write(pad + "]")
    else:
        out.write(json.dumps(value, separators=JSON_SEPARATORS))


def write_json(path, data):
    """Write data with its keys in order, through a temporary file, so an
    interrupted run never leaves a half JSON under the final name. Objects
    are indented by 2, a record or an event object takes one line, and an
    array of scalars or of short tuples takes one line."""
    rows = row_paths(data)
    partial = path + ".partial"
    try:
        with open(partial, "w") as out:
            _write_value(out, data, "", 0, rows)
            out.write("\n")
        os.replace(partial, path)
    finally:
        if os.path.exists(partial):
            os.remove(partial)

# SHARED END py-json-writer


# ============================================================================
# 12. Command line
# ============================================================================

def build_parser():
    parser = argparse.ArgumentParser(
        description="Read a Verilator VCD of CVA6 and write the CVA6Flow JSON "
                    "the viewer loads.")
    parser.add_argument(
        "vcd", help="The Verilator VCD of a CVA6 simulation (.vcd)")
    parser.add_argument(
        "-o", "--out", metavar="PATH",
        help="Where to write the JSON. Defaults to the VCD path with its .vcd "
             "extension replaced by .json")
    parser.add_argument(
        "--quiet", action="store_true",
        help="Leave out the progress line")
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit with 3 when metadata.degraded is not empty: a mechanism "
             "did not resolve, or the VCD was cut, holds no value changes, "
             "no rising edge, no instruction or no commit, starts part way "
             "through the run, or ends at a timestamp that disagrees with "
             "its cycle count. The JSON is "
             "still written. A cut exactly at a line boundary leaves no mark "
             "and can still pass. Use this in batch runs so a degraded VCD "
             "is not mistaken for a complete one")
    parser.add_argument(
        "--scope-prefix", default=DEFAULT_SCOPE_PREFIX, metavar="SCOPE",
        help=f"The hierarchy the signal table is looked up under. Defaults "
             f"to {DEFAULT_SCOPE_PREFIX}")
    parser.add_argument(
        "--disasm-list", metavar="PATH",
        help="An objdump -d -S -l listing of the test, the .list run_CVA6.py "
             "leaves in results/run/. Each record's disasm is filled by PC, "
             "and stays null outside the listing, the bootrom for one. "
             "Defaults to the VCD's path with .list when that file exists. "
             "The viewer refuses a JSON written without one")
    parser.add_argument(
        "--no-disasm-list", action="store_true",
        help="Do not look for a listing, and do not warn that there is none")
    parser.add_argument(
        "--config-name", default=DEFAULT_CONFIG_NAME, metavar="NAME",
        help=f"The CVA6 configuration the VCD was captured on, written to "
             f"metadata.config_name. Nothing in a VCD names its "
             f"configuration, so this is a label, not a measurement. "
             f"Defaults to {DEFAULT_CONFIG_NAME}, the one this tracer's "
             f"constants are written for, so name the configuration when "
             f"converting a sweep's VCDs")
    parser.add_argument(
        "--emit-diagnostics", action="store_true",
        help="Also write lsu_state_history and dc_event_log on every record, "
             "which no page reads. Off by default, which keeps a large JSON "
             "about 15%% smaller")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print how each mechanism bound its events to the records, on "
             "stderr")
    return parser


def default_out_path(vcd):
    """The VCD path with its .vcd extension replaced by .json."""
    return os.path.splitext(vcd)[0] + ".json"


def check_build(path_to_id, path_width, scope_prefix):
    """(exit status, sizes): 2 and None when the VCD's build is one this
    tracer would track wrongly, else 0 and (scoreboard entries, commit ports,
    writeback ports, D-cache sets or None)."""
    if probe_max_index(path_to_id, DECODED_PORT_PROBE) >= NR_ISSUE_PORTS:
        log_error("The VCD has decoded_instr_i[1], so the build is "
                  "superscalar, and this tracer follows one instruction per "
                  "issue port and a 32-bit fetch. The second port's "
                  "instructions would be lost and wraps_line would be wrong.")
        print("        Superscalar builds are not supported.",
              file=sys.stderr)
        return 2, None
    max_slot = probe_max_index(path_to_id, MEM_Q_SLOT_PROBE)
    if max_slot >= NR_SB_ENTRIES:
        # scoreboard.sv:323 requires a power of two.
        return refuse("mem_q slots", max_slot, NR_SB_ENTRIES,
                      "NR_SB_ENTRIES", 1 << max_slot.bit_length()), None
    max_commit = probe_max_index(path_to_id, COMMIT_PORT_PROBE)
    if max_commit >= NR_COMMIT_PORTS:
        return refuse("commit_pointer_q ports", max_commit, NR_COMMIT_PORTS,
                      "NR_COMMIT_PORTS", max_commit + 1), None
    max_wb = probe_max_index(path_to_id, WB_PORT_PROBE)
    if max_wb >= NR_WB_PORTS:
        return refuse("trans_id_i ports", max_wb, NR_WB_PORTS, "NR_WB_PORTS",
                      max_wb + 1), None
    # A probe that finds nothing would size its rows to zero, and the signals
    # the walk needs would silently become optional.
    absent = [family for family, largest in (
        ("mem_q[n].sbe.fu", max_slot), ("commit_pointer_q[n]", max_commit),
        ("trans_id_i[n]", max_wb)) if largest < 0]
    if absent:
        log_error(f"The VCD declares no {', '.join(absent)}, so the "
                  f"scoreboard cannot be followed and the JSON would be "
                  f"silently wrong.")
        print("        Dump the whole design, as run_CVA6.py does, and rerun.",
              file=sys.stderr)
        return 2, None
    dc_sets = probe_dcache_sets(path_width, scope_prefix)
    hpdcache = f"{scope_prefix}.{HPDCACHE_SCOPE}"
    if dc_sets is None and any(path.startswith(hpdcache)
                               for path in path_to_id):
        log_error(f"The VCD has an HPDcache but no {DCACHE_SET_SIGNAL}, so "
                  f"the D-cache set count that joins a writeback to its "
                  f"eviction is unknown.")
        print("        Dump the whole design, as run_CVA6.py does, and rerun.",
              file=sys.stderr)
        return 2, None
    # A largest index N means N + 1 slots or ports, NrCommitPorts included.
    return 0, (max_slot + 1, max_commit + 1, max_wb + 1, dc_sets)


def find_disasm_list(args):
    """The listing to read, or None, saying which and why."""
    if args.no_disasm_list:
        return None
    if args.disasm_list:
        if os.path.isfile(args.disasm_list):
            return args.disasm_list
        log_warn(f"--disasm-list {args.disasm_list} does not exist, so every "
                 f"record's disasm is null and the viewer refuses the JSON.")
        return None
    guess = os.path.splitext(args.vcd)[0] + ".list"
    if os.path.isfile(guess):
        log_info(f"Using {os.path.basename(guess)} for the disassembly. "
                 f"--disasm-list picks another, --no-disasm-list skips it.")
        return guess
    log_warn(f"No --disasm-list and no {os.path.basename(guess)} beside the "
             f"VCD, so every record's disasm is null and the viewer refuses "
             f"the JSON. run_CVA6.py leaves the listing in results/run/.")
    return None


def describe_vcd_clock(run, timescale):
    """The clock period the VCD itself implies, for checking the time base.
    The JSON assumes 50 MHz whatever this says."""
    period = run.vcd_clock_period
    unit_ps = timescale_ps(timescale)
    if period is None:
        log_info("VCD clock period: fewer than two rising edges.")
    elif unit_ps is None:
        log_info(f"VCD clock period: {period} units of a timescale that does "
                 f"not parse, {timescale!r}.")
    elif period * unit_ps < MIN_PLAUSIBLE_CLOCK_PS:
        log_info(f"VCD clock period: {period} units of {timescale}, too short "
                 f"for a clock, since Verilator advances its time two units a "
                 f"cycle. The JSON assumes 50 MHz.")
    else:
        period_ps = period * unit_ps
        log_info(f"VCD clock period: {period_ps / 1000:.3f} ns "
                 f"({1e6 / period_ps:.3f} MHz). The JSON assumes 50 MHz.")


def mispredict_census(records):
    """Where each bp_mispredict record went, once each: flushed, the causer
    of a bubble, resumed by the next record with no bubble, or cut by the end
    of the VCD. Anything left over fell through tag_bubbles."""
    ordered = sorted(records, key=lambda rec: rec.id)
    last_unflushed = max((i for i, rec in enumerate(ordered)
                          if not rec.flushed), default=-1)
    census = Counter()
    for i, rec in enumerate(ordered):
        if rec.bp_mispredict is not True:
            continue
        census["total"] += 1
        if rec.flushed:
            census["flushed"] += 1
        elif rec.caused_bubble_kind is not None:
            census["caused a bubble"] += 1
        elif i >= last_unflushed:
            census["ended with the VCD"] += 1
        elif not ordered[i + 1].flushed:
            census["resumed at once"] += 1
        else:
            census["unaccounted"] += 1
    return census


def print_verbose(run, mechanisms, records, n_high_bound, bindings,
                  n_unresolved_producers, table, ids):
    """The per-mechanism diagnostics --verbose asks for."""
    if not run.verbose:
        return
    pipeline = mechanisms.pipeline
    perf = mechanisms.perf
    for mechanism, resolved in mechanisms.resolved.items():
        state = "resolved" if resolved else "not resolved"
        log_info(f"Mechanism {mechanism}: {state}.")
    log_info(f"Signal table: {len(ids)} of {len(table)} rows found.")
    deliveries = mechanisms.icache.events
    n_rebound, n_synthesised, n_paired = bindings
    log_info(f"I-cache: {len(deliveries):,} deliveries "
             f"({sum(1 for e in deliveries if e.is_miss):,} misses), "
             f"{n_rebound:,} records rebound, {n_synthesised:,} synthesised "
             f"by the order repair, {n_paired:,} taken from a compressed "
             f"partner.")
    n_wraps = sum(1 for rec in records if rec.wraps_line)
    log_info(f"wraps_line: {n_wraps:,} records, {n_high_bound:,} with a "
             f"bound high fetch. The realigner served "
             f"{perf.n_unaligned_runs:,} unaligned runs over "
             f"{perf.n_unaligned_cycles:,} cycles.")
    counts = mechanisms.bubble_counts
    log_info("Bubbles. " + ", ".join(f"{kind} {n:,}"
                                     for kind, n in counts.items()) + ".")
    census = mispredict_census(records)
    log_info("Mispredicted records: " + ", ".join(
        f"{what} {n:,}" for what, n in census.items()) + ". Unaccounted "
        "must be absent.")
    loads = [rec for rec in records if rec.fu == "LOAD"]
    stores = [rec for rec in records if rec.fu == "STORE"]
    log_info(f"LSU FSMs: "
             f"{sum(1 for rec in loads if rec.lsu_state_history):,} of "
             f"{len(loads):,} loads and "
             f"{sum(1 for rec in stores if rec.lsu_state_history):,} of "
             f"{len(stores):,} stores tracked.")
    log_info(f"D-cache: {len(mechanisms.dcache.events):,} allocation, check "
             f"and refill response pulses, "
             f"{len(mechanisms.dcache.refill_active_cycles):,} "
             f"refill-active cycles.")
    wb_stats = mechanisms.mem_wb_stats
    log_info(f"Memory writebacks: {wb_stats['n_allocs']:,} allocations, "
             f"{wb_stats['n_matched_pairs']:,} request and response pairs, "
             f"{wb_stats['n_linked']:,} linked to their eviction.")
    # Committed records only, as metadata.stats.fwd counts them.
    via = Counter(getattr(rec, f"fwd_rs{n}_via") for rec in records
                  if not rec.flushed for n in (1, 2, 3))
    log_info(f"Forwarding: {pipeline.n_issue_cycles:,} issues, "
             f"{via['wb']:,} committed operands off the writeback bus, "
             f"{via['sb']:,} from the scoreboard, {n_unresolved_producers:,} "
             f"operands without a producer record.")
    log_info(f"Perf-counter access cycles: I-cache "
             f"{len(perf.ic_access_cycles):,}, D-cache "
             f"{len(perf.dc_access_cycles):,}. I-cache miss pulses "
             f"{len(perf.ic_miss_cycles):,}.")


def print_warnings(mechanisms, records):
    pipeline = mechanisms.pipeline
    if mechanisms.n_unknown_bit_reads:
        log_warn(f"{mechanisms.n_unknown_bit_reads:,} bit reads hit x or z "
                 f"in the VCD. The commit or writeback strobes they carried "
                 f"were unreadable, so the records behind them are missing.")
    for count, what in (
            (pipeline.n_unmatched_writebacks,
             "writeback(s) named a trans_id no in-flight record held"),
            (pipeline.n_unmatched_commits,
             "commit(s) named a trans_id no in-flight record held"),
            (pipeline.n_unmatched_decodes,
             "decode handshake(s) found no fetched record to issue"),
            (pipeline.n_unmatched_resolutions,
             "branch resolution(s) found no unresolved record with their "
             "PC")):
        if count:
            log_warn(f"{count:,} {what}. metadata.stats counts them.")
    if not records:
        log_warn("No CVA6 instruction was recovered, so the fetch handshake "
                 "never fired in the VCD. Check that the simulation ran past "
                 "reset.")


# SHARED BEGIN py-summary

# Needs: re, sys

TIME_UNIT_SECONDS = {"fs": 1e-15, "ps": 1e-12, "ns": 1e-9, "us": 1e-6,
                     "ms": 1e-3, "s": 1.0}


def clock_text(metadata):
    """The Clock period row, from the three keys both JSONs share."""
    period, unit = metadata["clock_period"], metadata["time_unit"]
    text = f"{period:,} units of {unit}"
    match = re.fullmatch(r"(\d+)(fs|ps|ns|us|ms|s)", unit or "")
    if match and period:
        seconds = (period * int(match.group(1))
                   * TIME_UNIT_SECONDS[match.group(2)])
        text += f" ({1 / seconds / 1e6:g} MHz)"
    return f"{text}, {metadata['clock_period_source']}"


def elapsed_text(seconds, input_bytes):
    """The Elapsed row, with the rate the input was read at."""
    rate = input_bytes / (1 << 20) / max(seconds, 1e-9)
    return f"{seconds:.1f}s ({rate:.1f} MiB/s)"


def print_summary_rows(title, rows):
    """The closing summary as aligned label : value rows on stdout, flushed
    so a log capturing both streams keeps it above the degraded report."""
    width = max(len(label) for label, _ in rows)
    print(title)
    for label, value in rows:
        print(f"  {label:<{width}} : {value}")
    sys.stdout.flush()

# SHARED END py-summary


def print_summary(args, out, run, records, mechanisms, metadata,
                  disasm_list_path, disasm_counts, elapsed):
    """The closing summary, on stdout so a batch log can keep it apart."""
    pipeline = mechanisms.pipeline
    size = os.path.getsize(args.vcd)
    n_drained = pipeline.n_drained_fetched + pipeline.n_drained_issued
    rows = [
        ("Input", args.vcd),
        ("Output", out),
        ("Records", f"{len(records):,}"),
        ("Committed", f"{pipeline.n_committed:,}"),
        ("Flushed", f"{sum(1 for rec in records if rec.flushed):,} (fetched "
                    f"{pipeline.n_flushed_fetched:,}, issued "
                    f"{pipeline.n_flushed_issued:,}, drained at the end "
                    f"{n_drained:,})"),
        ("Clock period", clock_text(metadata)),
        ("Compressed", f"{sum(1 for rec in records if rec.is_compressed):,}"),
        ("VCD size", human(size)),
        ("VCD lines", f"{run.n_lines:,}"),
        ("Value changes", f"{run.n_changes:,}"),
        ("Cycles", f"{run.n_cycles:,}"),
        ("Final timestamp", f"{run.last_ts:,}"),
    ]
    if disasm_list_path:
        n_annotated, n_no_pc, n_unmapped = disasm_counts
        rows += [("Disassembly listing", disasm_list_path),
                 ("Annotated records", f"{n_annotated:,}"),
                 ("Unmapped records", f"{n_unmapped:,}")]
        if n_no_pc:
            rows.append(("Records without a PC", f"{n_no_pc:,}"))
    if records:
        first = records[0]

        def reg(value):
            return "-" if value is None else f"x{value}"

        rows.append(("First record", (
            f"id {first.id}, pc {first.pc}, word {first.instr_word}, "
            f"{first.fu or '-'}, rd {reg(first.rd)}, rs1 {reg(first.rs1)}, "
            f"rs2 {reg(first.rs2)}")))
        if first.disasm:
            rows.append(("First record text", first.disasm))
        rows.append(("First record cycles", (
            f"fe out {first.fe_out_cycle}, dec {first.dec_cycle}, ex "
            f"{first.ex_cycle}, wb {first.wb_cycle}, co {first.co_cycle}")))
        committed = [rec for rec in records if not rec.flushed and rec.fu]
        categories = Counter(rec.fu_category for rec in committed)
        units = Counter(rec.fu for rec in committed)
        rows.append(("Committed by category", ", ".join(
            f"{name} {n:,}" for name, n in sorted(categories.items()))))
        rows.append(("Committed by FU", ", ".join(
            f"{name} {n:,}" for name, n in units.most_common())))
    rows.append(("Elapsed", elapsed_text(elapsed, size)))
    print_summary_rows("CVA6Flow tracer summary", rows)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not os.path.isfile(args.vcd):
        log_error(f"VCD not found: {args.vcd}")
        print("        Check the path and try again.", file=sys.stderr)
        return 1
    out = args.out or default_out_path(args.vcd)
    size = os.path.getsize(args.vcd)
    log_info(f"Reading {args.vcd} ({human(size)})")
    started = time.time()
    run = Run(verbose=args.verbose,
              progress=Progress("INFO", size, args.quiet))

    with open(args.vcd, "r", encoding="ascii", errors="replace") as f:
        path_to_id, path_width, timescale = parse_var_block(f)
        log_info(f"Header: {len(path_to_id):,} signals, timescale "
                 f"{timescale}")
        status, sizes = check_build(path_to_id, path_width, args.scope_prefix)
        if status:
            return status
        nr_sb_entries, nr_commit_ports, nr_wb_ports, dc_sets = sizes
        table = signal_table(nr_sb_entries, nr_commit_ports, nr_wb_ports)
        ids, missing = match_signal_table(table, path_to_id,
                                          args.scope_prefix)
        report_missing(table, missing, path_to_id)
        missing_core = [name for name in missing if "core" in table[name][1]]
        if missing_core:
            log_error(f"{len(missing_core)} required signal(s) are not in "
                      f"the VCD, so it cannot be walked: "
                      f"{', '.join(missing_core[:MAX_MISSING_REPORTED])}.")
            return 2
        resolved = resolved_mechanisms(table, ids)
        del resolved["core"]
        mechanisms = Mechanisms(resolved, nr_sb_entries, nr_commit_ports,
                                nr_wb_ports)
        sample = EdgeSample({}, ids)
        stream_and_extract(f, mechanisms, sample, run)
    run.progress.done()
    mechanisms.n_unknown_bit_reads = sample.n_unknown_bit_reads
    describe_vcd_clock(run, timescale)

    records = mechanisms.pipeline.completed
    index = index_events(mechanisms.icache.events)
    n_high_bound = match_records_to_events(records, index)
    n_rebound, n_synthesised = repair_fetch_order(records, index)
    bindings = (n_rebound, n_synthesised, pair_rvc_fetches(records))
    if resolved["lsu_fsm"] and resolved["dcache"]:
        attribute_dc_events(records, mechanisms.dcache)
    mem_writebacks, mechanisms.mem_wb_stats = finalise_mem_writebacks(
        mechanisms.mem_wb, dc_sets)
    disasm_list_path = find_disasm_list(args)
    disasm = parse_disasm_list(disasm_list_path) if disasm_list_path else {}
    disasm_counts = apply_disasm(records, disasm)
    n_unresolved_producers = forwarding_producers(records)
    branch_outcomes(records)
    mechanisms.bubble_counts = tag_bubbles(records)
    pre_fetch_waits(records)

    stats = build_stats(records, mechanisms, len(table), len(missing),
                        bindings, disasm_counts)
    degraded = build_degraded(resolved, run, mechanisms.pipeline)
    ic_events, dc_events = event_objects(mechanisms)
    try:
        data = {
            "metadata": build_metadata(args, run, timescale, dc_sets,
                                       disasm_list_path, degraded, stats),
            "config_params": build_config_params(
                nr_sb_entries, nr_commit_ports, nr_wb_ports, dc_sets),
            "instructions": record_dicts(records, args.emit_diagnostics),
            "ic_events": ic_events,
            "dc_events": dc_events,
            "mem_writebacks": mem_writebacks,
        }
        if tuple(data) != TOP_LEVEL_ORDER:
            raise ValueError("main drifted from TOP_LEVEL_ORDER")
        check_field_lists(data["instructions"], data["metadata"], CORE_NAMES)
    except ValueError as err:
        log_error(f"The JSON was not written: {err}.")
        return 1
    log_info(f"Writing {out}")
    write_json(out, data)

    print_verbose(run, mechanisms, records, n_high_bound, bindings,
                  n_unresolved_producers, table, ids)
    print_warnings(mechanisms, records)
    print_summary(args, out, run, records, mechanisms, data["metadata"],
                  disasm_list_path, disasm_counts, time.time() - started)
    print_degraded(degraded)
    return exit_status(degraded, args.strict, "VCD")


if __name__ == "__main__":
    sys.exit(main())
