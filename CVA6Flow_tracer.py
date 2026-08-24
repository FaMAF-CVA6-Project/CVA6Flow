#!/usr/bin/env python3
"""CVA6 pipeline tracer. Extracts per-instruction lifecycle data from a
Verilator VCD and emits JSON for the CVA6Flow viewer, following each
instruction from fetch through decode, issue, execute, writeback and commit.

Usage:
    python3 CVA6Flow_tracer.py <path-to.vcd>
    python3 CVA6Flow_tracer.py daxpy.vcd --output daxpy.json
"""

import argparse
import bisect
import json
import re
import sys
import time
from statistics import median
from collections import deque, defaultdict, Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ============================================================================
# Loading / progress output (mirrors the MinorFlow tracer)
# ============================================================================
_SHOW_STAGES = False
_PROG = None


def stagelog(*args, **kwargs):
    """Per-stage resolution diagnostics. Silent unless --stages is given."""
    if _SHOW_STAGES:
        print(*args, **kwargs)


class Progress:
    """In-place stderr progress reporter for the streaming parse. Prints e.g.
    "[parse] 14,250,000 lines \u00b7 312,004 insts \u00b7 18.3s" on a single
    rewritten line, throttled to a few times a second."""

    def __init__(self, label, enabled=True):
        self.label = label
        self.enabled = enabled and sys.stderr.isatty()
        self.force_plain = enabled and not sys.stderr.isatty()
        self.start = time.time()
        self.last_emit = 0.0
        self.lines = 0
        self.insts = 0

    def update(self, lines, insts=0, final=False):
        self.lines = lines
        self.insts = insts
        now = time.time()
        if not final and (now - self.last_emit) < 0.25:
            return
        self.last_emit = now
        elapsed = now - self.start
        msg = (f"[{self.label}] {lines:,} lines \u00b7 {insts:,} insts "
               f"\u00b7 {elapsed:.1f}s")
        if self.enabled:
            sys.stderr.write("\r" + msg + "   ")
            sys.stderr.flush()
        elif self.force_plain and (final or int(elapsed) % 5 == 0):
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()

    def done(self):
        self.update(self.lines, self.insts, final=True)
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()


# ============================================================================
# Config (single source of truth)
# ============================================================================
# Values come from cv64a6_imafdc_sv39_hpdcache_wb_config_pkg.sv and
# build_config_pkg.sv. Changing one here regenerates every config-dependent
# signal path. The FSM enums and sid table are CVA6-wide and do not vary.

# Frontend
SUPERSCALAR_EN = False
RVC_EN = True
FETCH_WIDTH = 32                        # bits (=64 when SuperscalarEn=1)
FETCH_BYTES = FETCH_WIDTH // 8
FETCH_OFFSET_MASK = FETCH_BYTES - 1           # 0x3 for FW=32, 0x7 for FW=64
INSTR_PER_FETCH = FETCH_WIDTH // (16 if RVC_EN else 32)

# Backend
NR_ISSUE_PORTS = 1
NR_COMMIT_PORTS = 2
NR_WB_PORTS = 5
NR_SB_ENTRIES = 8
TRANS_ID_BITS = 3                         # = $clog2(NR_SB_ENTRIES)

# LSU. ex_stage has three dcache_req_ports_o slots, a CVA6-wide constant:
# port 0 load adapter, 1 MMU/PTW, 2 store adapter (ex_stage.sv and
# cva6.sv:1326). It does not vary with the scoreboard or issue-port config.
DCACHE_REQ_PORTS = 3


# ============================================================================
# I$ controller FSM enum
# ============================================================================
# Mirrors cva6_icache.sv:122. Used by ICacheTimeline.on_cycle to classify
# each delivery as a hit (state_q == READ at fe2) or miss (state_q == MISS).
# VCD encodes the 3-bit state as a binary string, e.g. "011" for MISS.

FSM_FLUSH = "000"
FSM_IDLE = "001"
FSM_READ = "010"
FSM_MISS = "011"
FSM_KILL_ATRANS = "100"
FSM_KILL_MISS = "101"


# ============================================================================
# LSU FSM enums
# ============================================================================
# load_unit.sv:83 . 4-bit FSM, 9 states
# store_unit.sv:119. 2-bit FSM, 4 states
#
# A SystemVerilog enum with no explicit values counts up from 0, and the VCD
# encodes each as a binary string of the declared width, 4 chars for
# load_unit and 2 for store_unit. Names lifted verbatim from the source.

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


# ============================================================================
# Control-flow type enum (branch predictor)
# ============================================================================
# Per ariane_pkg.sv:170-176. The cf_t type is used both as the prediction
# carried with each instruction (branchpredict_sbe_t.cf) and as the
# resolution emitted by the branch_unit (bp_resolve_t.cf_type).
#
#   NoCF   = 0  : nothing predicted. Also the value for a branch
#                 predicted not-taken. Branch = 1 BHT, Jump = 2 direct,
#                 JumpR = 3 BTB indirect, Return = 4 RAS.
#
# A cf value at issue also names the predictor: Branch is the BHT, JumpR the
# BTB, Return the RAS. Jump needs none, the decoder resolves it, and NoCF
# means no prediction or a non-branch.

CF_T_NAMES = {
    0: "NoCF",
    1: "Branch",
    2: "Jump",
    3: "JumpR",
    4: "Return",
}


def cf_name(s):
    """Decode a cf_t binary string. Returns 'NoCF' on None/unknown."""
    if s is None:
        return None
    v = binary_to_int(s)
    if v is None:
        return None
    return CF_T_NAMES.get(v, f"UNK_{v}")


# ============================================================================
# HPDcache requestor source-ID assignment
# ============================================================================
# Per cva6_hpdcache_wrapper.sv (NumPorts=4 in
# cv64a6_imafdc_sv39_hpdcache_wb_config_pkg.sv) the SID layout is:
#
#   sid 0 PTW load adapter, 1 LSU load_unit (the one we care about),
#   2 accelerator load adapter, 3 STORE adapter, 4 CMO adapter,
#   5 hwpf_stride prefetcher.
#
# Traced through cva6.sv:1321-1327 into load_store_unit.sv:315/586/545. The
# HPDcache wrapper feeds dcache_req_ports_i[0..2] to load adapter slots
# r=0..2 with hpdcache_req_sid_i = r.

HPDCACHE_NUM_PORTS = 4
LOAD_ADAPTER_SIDS = frozenset(range(HPDCACHE_NUM_PORTS - 1))   # {0, 1, 2}
PTW_LOAD_SID = 0
LOAD_UNIT_SID = 1   # ← the only SID that flips dc_primary_miss on a LOAD record
ACCEL_LOAD_SID = 2
STORE_ADAPTER_SID = HPDCACHE_NUM_PORTS - 1                     # 3
CMO_ADAPTER_SID = HPDCACHE_NUM_PORTS                          # 4
HWPF_ADAPTER_SID = HPDCACHE_NUM_PORTS + 1                      # 5

# REFILL_FSM from hpdcache_miss_handler.sv, widened to 32 bits by Verilator
# because the typedef has no explicit width (line 397). State 0 is idle, any
# non-zero value means a refill is in progress.
REFILL_FSM_IDLE = 0


# ============================================================================
# Whitelist (base set + commit_pointer_q for trans_id-based commit matching)
# ============================================================================

WHITELIST = [
    # Clock
    "clk_i",

    # CSR-equivalent D$ access counter, ports 0 load, 1 MMU/PTW, 2 store.
    # Sampled at ex_stage's output, what perf_counters.sv:128 reads. NOT the
    # cache-side ports, whose port 2 is the accelerator (cva6.sv:1326).

    # I$ request / response
    "i_frontend.icache_dreq_o.req",
    "i_frontend.icache_dreq_o.vaddr",
    "i_frontend.icache_dreq_o.kill_s1",
    "i_frontend.icache_dreq_o.kill_s2",
    "i_frontend.icache_dreq_i.valid",
    "i_frontend.icache_dreq_i.vaddr",

    # instr_realign output flag, high while the realigner combines an
    # instruction split across two fetches. Cross-checks wraps_line, and
    # differs only for records flushed mid-realignment by kill_s2.
    "i_frontend.i_instr_realign.serving_unaligned_o",

    # Fetch handshake
    "id_stage_i.fetch_entry_valid_i",
    "id_stage_i.fetch_entry_ready_o",
    "id_stage_i.rvfi_is_compressed_o",

    # Per-instruction payload from frontend. `fetch_entry_if_id` is
    # declared [NrIssuePorts-1:0]. Ports appended programmatically.

    # Decode handshake
    "issue_stage_i.i_scoreboard.decoded_instr_valid_i",
    "issue_stage_i.i_scoreboard.decoded_instr_ack_o",

    # Issue handshake
    "issue_stage_i.i_scoreboard.issue_instr_valid_o",
    "issue_stage_i.i_scoreboard.issue_ack_i",
    "issue_stage_i.i_scoreboard.issue_pointer_q",

    # Decoded fields at the decode handshake, per-port entries appended
    # below. bp comes from here, not mem_q[tid].sbe.bp, whose slot holds the
    # previous occupant post-edge once issue_pointer_q has advanced.

    # Forwarding capture. Probed at the issue cycle to learn
    # whether each source operand was taken from the regfile or from
    # the forwarding network, and from which producer slot.
    #
    # forward_rsX is 1 when the source had a RAW hazard and the operand was
    # available on the forwarding network. idx_hzd_rsX is the scoreboard slot
    # it forwards from, meaningful only when forward_rsX is 1.
    #
    # All six are [NrIssuePorts-1:0] in issue_read_operands. forward_rsX is one
    # bit per port, idx_hzd_rsX is TRANS_ID_BITS per port, and the per-port
    # idx_hzd_rs slices are appended programmatically below.
    "issue_stage_i.i_issue_read_operands.forward_rs1",
    "issue_stage_i.i_issue_read_operands.forward_rs2",
    "issue_stage_i.i_issue_read_operands.forward_rs3",

    # Writeback. Wt_valid_i is a packed NR_WB_PORTS-bit bus. Per-port
    # trans_id_i slices are appended programmatically below.
    "issue_stage_i.i_scoreboard.wt_valid_i",

    # The scoreboard's registered mem_q ring. Reading fu/rs1/rs2/rd from
    # mem_q[trans_id].sbe at writeback is authoritative, written at the decode
    # edge and stable until the slot is reused. Per-slot entries appended below.

    # bp.cf and bp.predict_address come from mem_q[trans_id].sbe.bp at
    # writeback, the same data commit uses. The pre-edge decoded_instr_i.bp
    # snapshot is the fallback when mem_q is absent or the record is flushed.

    # Branch resolution from the EX branch_unit. bp_resolve_t (cva6.sv:134)
    # carries pc, target, is_taken, is_mispredict and cf_type for one cycle at
    # ex_cycle. Bound by PC, oldest is_cycle first when a loop repeats a PC.
    "issue_stage_i.i_scoreboard.resolved_branch_i.valid",
    "issue_stage_i.i_scoreboard.resolved_branch_i.pc",
    "issue_stage_i.i_scoreboard.resolved_branch_i.target_address",
    "issue_stage_i.i_scoreboard.resolved_branch_i.is_taken",
    "issue_stage_i.i_scoreboard.resolved_branch_i.is_mispredict",
    "issue_stage_i.i_scoreboard.resolved_branch_i.cf_type",

    # Commit. commit_ack_o is a packed NR_COMMIT_PORTS-bit bus. The
    # per-port commit_pointer_q slices (tagging the trans_id released
    # on each port) are appended programmatically below.
    "commit_stage_i.commit_ack_o",

    # Flush
    "flush_ctrl_if",
    "flush_ctrl_id",
    "flush_ctrl_ex",
    "flush_ctrl_bp",
    # flush_unissued_instr_i gates the scoreboard's mem_n write at the decode
    # handshake (scoreboard.sv:171). While it is high DV and DA still fire but
    # no slot is allocated, so firing here would drift `fetched` ahead of HW.
    "issue_stage_i.i_scoreboard.flush_unissued_instr_i",

    # I$ controller FSM, the 6-state enum at cva6_icache.sv:122. Separates a
    # hit (READ at fe2) from a line miss (MISS at fe2). The frontend dreq
    # signals above carry the handshake, this one the internal state.
    "gen_cache_hpd.i_cache_subsystem.i_cva6_icache.state_q",

    # I$ miss pulse. cva6_icache asserts miss_o for one cycle per accepted
    # cacheable ifill (cva6_icache.sv:301-303), feeding perf_counters event 1.
    # Counting its high cycles includes wrong-path fills squashed mid-fill.
    "gen_cache_hpd.i_cache_subsystem.i_cva6_icache.miss_o",

    # LSU pipeline FSM state registers, sampled per rising edge and attributed
    # to the pending record set by the issue handshake. load_unit.sv:83 has 9
    # states in 4 bits, store_unit.sv:119 has 4 in 2 bits.
    "ex_stage_i.lsu_i.i_load_unit.state_q",
    "ex_stage_i.lsu_i.i_store_unit.state_q",
    # lsu_ctrl is the combinational wire feeding both
    # FSMs (load_store_unit.sv:174). Its trans_id at the cycle
    # BEFORE an FSM IDLE→non-IDLE transition is the admitted record.
    "ex_stage_i.lsu_i.lsu_ctrl.trans_id",
    # pop_ld / pop_st fire when the load or store unit consumes a request
    # from lsu_bypass. pop_ld in SEND_TAG (load_unit.sv:343) or pop_st in
    # VALID_STORE (store_unit.sv:191) is an admit-while-busy event.
    "ex_stage_i.lsu_i.lsu_bypass_i.pop_ld_i",
    "ex_stage_i.lsu_i.lsu_bypass_i.pop_st_i",

    # HPDcache miss and refill events. cva6.sv has three cache-subsystem
    # variants under different gen_cache_* blocks, and this build uses
    # HPDcache, so everything sits under gen_cache_hpd.
    #
    # The mshr_alloc_* group is sampled when mshr_alloc_i pulses, a primary
    # miss. mshr_alloc_sid_i names the requestor and is the only way to tell a
    # load-adapter miss from a store or prefetch one.
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_alloc_i",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_alloc_tid_i",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_alloc_sid_i",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_alloc_is_prefetch_i",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_alloc_nline_i",
    # mshr_check_i with mshr_check_hit_o on the same cycle is the secondary
    # miss path, a request whose nline is already in an MSHR entry. This is
    # the dominant path where loads follow stores to the same line.
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_check_i",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_check_nline_i",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_check_hit_o",
    # refill_fsm_q non-zero flags loads that overlap a refill without being
    # part of the alloc or coalesce. id=142 in fdiv was a hit delayed by a
    # refill holding the cache port.
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.refill_fsm_q",
    # refill_core_rsp_valid_o pulses when refill data is delivered
    # back to the requesting core port. Refill_core_rsp_o.tid carries
    # the requesting tid (see hpdcache_miss_handler.sv:382,397).
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.refill_core_rsp_valid_o",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.refill_core_rsp_o.tid",

    # Dirty victim writeback, write-back with the write buffer configured
    # out. ALLOC is flush_alloc, SEND and ACK are mem_req/resp_write_flush.
    # Send and ack pair by slot id, alloc and ack by nline.
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.flush_alloc",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.flush_alloc_ready",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.flush_alloc_nline",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.mem_req_write_flush_valid",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.mem_req_write_flush_ready",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.mem_req_write_flush.mem_req_id",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.mem_req_write_flush.mem_req_addr",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.mem_resp_write_flush_valid",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.mem_resp_write_flush_ready",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.mem_resp_write_flush.mem_resp_w_id",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.flush_ack_nline",

    # Tie each writeback to its eviction. st2 drives flush_alloc with the miss
    # allocation on the same cycle, and they join on (set, victim_way), with
    # 256 sets and 8 ways giving set = nline & 0xff.
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_alloc_wback_i",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    "hpdcache_miss_handler_i.mshr_alloc_victim_way_i",
    "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache.flush_alloc_way",
]

# ------------------------------------------------------------------ #
# Loop-generated per-port / per-entry signal paths.                  #
#                                                                    #
# All hardcoded indexed signals from the original WHITELIST were     #
# moved here so the per-port arrays scale with the constants at the  #
# top of this file. Adding a port needs only that one change.        #
# ------------------------------------------------------------------ #

# ex_stage dcache request ports (architectural, NOT scoreboard-derived)
for _p in range(DCACHE_REQ_PORTS):
    WHITELIST.append(f"ex_stage_i.dcache_req_ports_o[{_p}].data_req")

# fetch_entry_if_id (per NrIssuePorts)
for _p in range(NR_ISSUE_PORTS):
    WHITELIST += [
        f"fetch_entry_if_id[{_p}].address",
        f"fetch_entry_if_id[{_p}].instruction",
    ]

# decoded_instr_i (per NrIssuePorts × {fu, rs1, rs2, rd, bp.cf, bp.predict_address})
for _p in range(NR_ISSUE_PORTS):
    WHITELIST += [
        f"issue_stage_i.i_scoreboard.decoded_instr_i[{_p}].fu",
        f"issue_stage_i.i_scoreboard.decoded_instr_i[{_p}].rs1",
        f"issue_stage_i.i_scoreboard.decoded_instr_i[{_p}].rs2",
        f"issue_stage_i.i_scoreboard.decoded_instr_i[{_p}].rd",
        f"issue_stage_i.i_scoreboard.decoded_instr_i[{_p}].bp.cf",
        f"issue_stage_i.i_scoreboard.decoded_instr_i[{_p}].bp.predict_address",
    ]

# idx_hzd_rs{1,2,3} (per NrIssuePorts)
for _p in range(NR_ISSUE_PORTS):
    for _rs in (1, 2, 3):
        WHITELIST.append(
            f"issue_stage_i.i_issue_read_operands.idx_hzd_rs{_rs}[{_p}]")

# trans_id_i (per NrWbPorts)
for _p in range(NR_WB_PORTS):
    WHITELIST.append(f"issue_stage_i.i_scoreboard.trans_id_i[{_p}]")

# mem_q, per NrScoreboardEntries over fu, rs1, rs2, rd, bp.cf and
# bp.predict_address. bp comes from the registered slot so the writeback fixup
# avoids the pre-edge misattribution on back-to-back issues.
for _i in range(NR_SB_ENTRIES):
    for _f in ("fu", "rs1", "rs2", "rd", "bp.cf", "bp.predict_address"):
        WHITELIST.append(
            f"issue_stage_i.i_scoreboard.mem_q[{_i}].sbe.{_f}")

# commit_pointer_q (per NrCommitPorts)
for _p in range(NR_COMMIT_PORTS):
    WHITELIST.append(f"issue_stage_i.i_scoreboard.commit_pointer_q[{_p}]")

del _p, _rs, _i, _f  # keep module namespace clean

REQUIRED_SIGNALS = {
    "clk_i",
    "id_stage_i.fetch_entry_valid_i",
    "id_stage_i.fetch_entry_ready_o",
    "fetch_entry_if_id[0].address",
    "fetch_entry_if_id[0].instruction",
    "issue_stage_i.i_scoreboard.decoded_instr_valid_i",
    "issue_stage_i.i_scoreboard.decoded_instr_ack_o",
    "issue_stage_i.i_scoreboard.decoded_instr_i[0].fu",
    "issue_stage_i.i_scoreboard.decoded_instr_i[0].rs1",
    "issue_stage_i.i_scoreboard.decoded_instr_i[0].rs2",
    "issue_stage_i.i_scoreboard.decoded_instr_i[0].rd",
    "issue_stage_i.i_scoreboard.issue_instr_valid_o",
    "issue_stage_i.i_scoreboard.issue_ack_i",
    "issue_stage_i.i_scoreboard.issue_pointer_q",
    "issue_stage_i.i_scoreboard.wt_valid_i",
    "issue_stage_i.i_scoreboard.trans_id_i[0]",
    "commit_stage_i.commit_ack_o",
}


# ============================================================================
# Functional-unit metadata (from ariane_pkg.sv fu_t enum + spec §5.7 rollup)
# ============================================================================

FU_NAME = {
    0:  "NONE",
    1:  "LOAD",
    2:  "STORE",
    3:  "ALU",
    4:  "CTRL_FLOW",
    5:  "MULT",
    6:  "CSR",
    7:  "FPU",
    8:  "FPU_VEC",
    9:  "CVXIF",
    10: "ACCEL",
    11: "AES",
}

# Per spec §5.7. MemFP (FP load/store) requires looking at the op or
# is_rd_fpr/is_rs2_fpr flag, not just fu. Deferred to a later increment.
# Both LOAD and STORE roll up to Mem regardless of int/FP target here.
FU_CATEGORY = {
    "ALU":       "Int",
    "CTRL_FLOW": "Int",
    "MULT":      "Int",
    "CSR":       "Int",
    "AES":       "Int",     # AES extensions execute on the FLU/AES unit
    "LOAD":      "Mem",
    "STORE":     "Mem",
    "FPU":       "FP",
    "FPU_VEC":   "FP",
    "CVXIF":     "CVXIF",
    "ACCEL":     "ACCEL",
    "NONE":      "None",
}


# ============================================================================
# Instruction record
# ============================================================================

@dataclass
class InstructionRecord:
    id: int = 0
    pc: str = None
    instr_word: str = None
    disasm: str = None
    is_compressed: bool = False
    fu: str = None
    fu_category: str = None
    rd: int = None
    rs1: int = None
    rs2: int = None
    trans_id: int = None
    fetch_port: int = 0
    # Request-accept and data-delivery cycles of the fetch carrying the low
    # half of the instruction. For an aligned instruction this is the only
    # fetch. On a hit if2 = if1 + 1, on a cacheable miss about if1 + 5.
    if1_lo: int = None
    if2_lo: int = None
    # Same for the fetch carrying the high half. Only set when wraps_line,
    # since an RVI at the last 2-byte slot of its fetch block has its upper
    # 16 bits in the next block and the realigner combines the two.
    if1_hi: int = None
    if2_hi: int = None
    # The instruction straddles a fetch-block boundary: pc at FETCH_BYTES-2
    # of its block and not compressed. Set on flushed records too, since the
    # realigner does its bookkeeping whether or not the instruction commits.
    wraps_line: bool = False
    # The I$ went to memory for this PC's line, state_q == MISS at if2.
    # False for a hit, including one queued behind an earlier miss.
    ic_miss: bool = None
    # Hi-side miss, from the same signal at the hi fetch's fe2 cycle. Only
    # meaningful when wraps_line, None when there is no hi fetch.
    ic_miss_hi: bool = None
    fe_cycle: int = None
    id_cycle: int = None
    is_cycle: int = None
    ex_cycle: int = None
    wb_cycle: int = None
    co_cycle: int = None
    flushed: bool = False
    flush_reason: str = None
    # Transitions of load_unit.state_q, or store_unit.state_q for stores,
    # while the FSM held this trans_id. Entries are {cycle, state}, and only
    # transitions are kept: the closing IDLE goes to lsu_complete_cycle.
    lsu_state_history: list = None
    # Cycle the LSU FSM left IDLE for this record, the admission cycle.
    # Usually is_cycle + 1, later under stalls or a TLB miss insert.
    lsu_admit_cycle: int = None
    # Cycle the FSM returned to IDLE. For a load the data arrives later via
    # ldbuf, so this marks the FSM's release rather than completion.
    lsu_complete_cycle: int = None

    # D$ correlation for LOAD and STORE records, filled by
    # attribute_dc_events_to_records() from the events that fired during
    # [lsu_admit_cycle, lsu_complete_cycle].

    # An mshr_alloc with sid == LOAD_UNIT_SID and mTID == this trans_id.
    # Rare when stores allocate first and loads coalesce behind them.
    dc_primary_miss: bool = False
    # An mshr_check_hit fired in this record's window, so something
    # coalesced onto an existing MSHR. Approximate, no per-check sid.
    dc_coalesced: bool = False
    # refill_fsm_q non-IDLE for at least one cycle of the window, catching
    # loads delayed by a concurrent refill holding the cache port.
    dc_refill_overlap: bool = False
    # Chronological events, each with 'cycle' and 'type' (alloc, check_hit,
    # check_miss, refill_rsp) plus that type's own fields.
    dc_events: list = None

    # Branch prediction and resolution, for records with fu == CTRL_FLOW.

    # "NoCF", "Branch", "Jump", "JumpR" or "Return", which also names the
    # source: Branch is the BHT, JumpR the BTB, Return the RAS, Jump direct.
    # Captured at the issue handshake from mem_q[trans_id].sbe.bp.
    bp_predicted_cf: str = None
    # VLEN-bit target as int, None for NoCF.
    bp_predicted_target: int = None
    # Resolved type per branch_unit.sv:64-107. May differ from the
    # predicted one when the BTB or RAS missed. Captured from
    # resolved_branch_i, which pulses for one cycle at ex_cycle.
    bp_resolved_cf: str = None
    bp_resolved_target: int = None
    # Actual outcome, False for not-taken.
    bp_resolved_taken: bool = None
    # is_mispredict straight from resolution, covering both a wrong
    # direction and a wrong target.
    bp_mispredict: bool = None
    # Cycle resolved_branch_i.valid fired. Equals ex_cycle for a cleanly
    # pipelined branch.
    bp_resolution_cycle: int = None

    # Operand forwarding, snapshotted pre-edge at the issue cycle because
    # issue_read_operands' signals are combinational and live for one cycle.
    # rs3 only applies to FMA-class FPU ops, and is None elsewhere.

    # The issue stage took the operand from the forwarding network rather
    # than the regfile, from i_issue_read_operands.forward_rsX. False also
    # covers a stall, since capture only runs on a successful handshake.
    fwd_rs1_used: bool = False
    # Scoreboard slot the operand came from, read from idx_hzd_rsX[0].
    # Only meaningful when the matching _used is True.
    fwd_rs1_from_tid: int = None
    # "sb" when the producer had already written back and the scoreboard
    # holds the result, "wb" when it was bypassed on the same cycle from a
    # writeback port. "wb" is the tight back-to-back dependent case.
    fwd_rs1_via: str = None
    fwd_rs2_used: bool = False
    fwd_rs2_from_tid: int = None
    fwd_rs2_via: str = None
    fwd_rs3_used: bool = False
    fwd_rs3_from_tid: int = None
    fwd_rs3_via: str = None

    # Branch and flush bubbles, tagged by tag_branch_bubbles() after the walk.
    # A non-flushed record followed by flushed ones then another non-flushed
    # one is the CAUSER, and the one that resumes is the RECOVERY.

    # On the causer. 'mispred' when the predictor guessed and was wrong,
    # 'unpred' when it said NoCF, 'flush_other' for a non-branch cause such
    # as a CSR write, a FENCE, an AMO commit drain or an exception entry.
    bubble_kind: str = None
    # Flushed records strictly between causer and recovery, which is the
    # count of wrong-path instructions that consumed fetch bandwidth.
    bubble_caused_cycles: int = None
    # Id of the recovery record, for cross-record joins.
    bubble_recovery_id: int = None
    # On the recovery. Id of the causer, and a copy of its
    # bubble_caused_cycles so either end can be queried without a join.
    bubble_from_branch_id: int = None
    bubble_cycles: int = None


# ============================================================================
# I$ event timeline
# ============================================================================

@dataclass
class ICacheEvent:
    fe1_cycle: int
    fe2_cycle: int
    vaddr_word: int   # 4-byte aligned
    ic_miss: bool


class ICacheTimeline:
    """Walks the VCD's I$ signals alongside the main tracer and emits one
    ICacheEvent per delivered fetch. A new access starts when vaddr_o changes
    or state_q re-enters READ, the second catching a re-fetch of the same PC."""

    NON_READ_STATES = frozenset({
        FSM_FLUSH, FSM_IDLE, FSM_MISS, FSM_KILL_ATRANS, FSM_KILL_MISS, None,
    })

    def __init__(self):
        self.events = []
        self.last_vaddr_o_str = None
        self.last_state_q = None
        self.last_access_start_cycle = None

    def on_cycle(self, cycle, state_q_str, vld, vaddr_o_str, k2):
        """Process one rising clock edge."""

        # --- Detect new access (either path) ---
        vaddr_o_changed = (vaddr_o_str != self.last_vaddr_o_str)
        state_to_read = (state_q_str == FSM_READ
                         and self.last_state_q in self.NON_READ_STATES)

        if vaddr_o_changed or state_to_read:
            self.last_access_start_cycle = cycle

        self.last_vaddr_o_str = vaddr_o_str
        self.last_state_q = state_q_str

        # --- Emit event on delivery ---
        if vld == "1" and k2 != "1" and vaddr_o_str is not None:
            try:
                vaddr_o = int(vaddr_o_str, 2)
            except ValueError:
                return
            if self.last_access_start_cycle is not None:
                fe1 = self.last_access_start_cycle - 1
            else:
                fe1 = cycle - 1
            fe1 = max(0, fe1)
            ic_miss = (state_q_str == FSM_MISS)
            self.events.append(ICacheEvent(
                fe1_cycle=fe1,
                fe2_cycle=cycle,
                vaddr_word=vaddr_o & ~FETCH_OFFSET_MASK,
                ic_miss=ic_miss,
            ))


def match_records_to_events(records, events):
    """Bind I$ timing onto each record: if1_lo, if2_lo and ic_miss from the
    event for the record's own word with the highest fe2 at or before fe_cycle,
    plus if1_hi and if2_hi at pc+2 when it wraps. No match leaves them None."""

    by_word = defaultdict(list)
    for ev in events:
        by_word[ev.vaddr_word].append(ev)
    # fe2_cycle keys per word, sorted alongside the events so each lookup can
    # binary-search its window. A hot loop puts one event per iteration on a
    # word, which made the old linear scan quadratic and could hang.
    by_word_fe2 = {}
    for word in by_word:
        by_word[word].sort(key=lambda e: e.fe2_cycle)
        by_word_fe2[word] = [e.fe2_cycle for e in by_word[word]]

    def find_best(word, fe_cycle):
        candidates = by_word.get(word, [])
        if not candidates:
            return None
        idx = bisect.bisect_right(by_word_fe2[word], fe_cycle) - 1
        if idx < 0:
            return None
        return candidates[idx]

    n_matched = 0
    n_unmatched = 0
    n_wraps_with_hi = 0

    for rec in records:
        if rec.pc is None or rec.fe_cycle is None:
            n_unmatched += 1
            continue
        try:
            pc_int = int(rec.pc, 16)
        except (TypeError, ValueError):
            n_unmatched += 1
            continue
        # First fetch: word containing the LOWER half of the instr.
        lo_word = pc_int & ~FETCH_OFFSET_MASK
        best_lo = find_best(lo_word, rec.fe_cycle)
        if best_lo is not None:
            rec.if1_lo = best_lo.fe1_cycle
            rec.if2_lo = best_lo.fe2_cycle
            rec.ic_miss = best_lo.ic_miss
            n_matched += 1
        else:
            n_unmatched += 1
            # Flushed-record fallback. A wrong-path fetch is killed before
            # the icache responds, so no event exists. Synthesize cycles two
            # before fe_cycle so the timeline draws if1/if2, not an orphan.
            if rec.flushed and rec.fe_cycle is not None:
                rec.if2_lo = max(0, rec.fe_cycle - 1)
                rec.if1_lo = max(0, rec.fe_cycle - 2)
                rec.ic_miss = False
        # Second fetch, wraps_line records only, upper half at pc+2.
        # fe1_cycle >= rec.if1_lo or find_best picks a stale prefetch, the
        # frontend being non-blocking on the hi side.
        if rec.wraps_line:
            hi_word = (pc_int + 2) & ~FETCH_OFFSET_MASK
            hi_candidates = by_word.get(hi_word, [])
            best_hi = None
            lo_floor = rec.if1_lo if rec.if1_lo is not None else 0
            hi_fe2 = by_word_fe2.get(hi_word, [])
            hi_top = bisect.bisect_right(hi_fe2, rec.fe_cycle)
            hi_start = bisect.bisect_left(hi_fe2, lo_floor)
            for i in range(hi_top - 1, hi_start - 1, -1):
                ev = hi_candidates[i]
                if ev.fe1_cycle >= lo_floor:
                    best_hi = ev
                    break
            if best_hi is not None:
                rec.if1_hi = best_hi.fe1_cycle
                rec.if2_hi = best_hi.fe2_cycle
                # Authoritative hi-miss from the icache FSM. The hi event
                # carries the same ic_miss bit as the lo one, replacing an
                # if2-if1 heuristic that over-counted on port-busy stalls.
                rec.ic_miss_hi = best_hi.ic_miss
                n_wraps_with_hi += 1

    # Enforce fetch monotonicity in commit order. find_best takes the latest
    # event with fe2 <= fe_cycle, so in a loop whose line stays cached an
    # iteration can bind to the previous iteration's delivery.
    #
    #   1. Rebind to a later event for the same word with fe1 >= prev_if1.
    #   2. Failing that, synthesize cycles just before id_stage entry and
    #      assume a cached hit, which respects program-order fetch.
    prev_if1 = -1
    prev_rec_with_if1 = None
    n_rebound = 0
    n_synth = 0
    for rec in records:
        if rec.if1_lo is None:
            continue
        if rec.if1_lo >= prev_if1:
            prev_if1 = rec.if1_lo
            prev_rec_with_if1 = rec
            continue
        # Violation. First try rebind.
        if rec.pc is None or rec.fe_cycle is None:
            prev_if1 = max(prev_if1, rec.if1_lo)
            continue
        try:
            pc_int = int(rec.pc, 16)
        except (TypeError, ValueError):
            prev_if1 = max(prev_if1, rec.if1_lo)
            continue
        lo_word = pc_int & ~FETCH_OFFSET_MASK
        candidates = by_word.get(lo_word, [])
        new_ev = None
        lo_fe2 = by_word_fe2.get(lo_word, [])
        lo_start = bisect.bisect_left(lo_fe2, prev_if1)
        lo_top = bisect.bisect_right(lo_fe2, rec.fe_cycle)
        for i in range(lo_start, lo_top):
            ev = candidates[i]
            if ev.fe1_cycle >= prev_if1:
                new_ev = ev
                break
        if new_ev is not None:
            rec.if1_lo = new_ev.fe1_cycle
            rec.if2_lo = new_ev.fe2_cycle
            rec.ic_miss = new_ev.ic_miss
            n_rebound += 1
            # Re-evaluate hi side too if this is a wraps_line record.
            if rec.wraps_line:
                hi_word = (pc_int + 2) & ~FETCH_OFFSET_MASK
                hi_candidates = by_word.get(hi_word, [])
                new_hi = None
                hi_fe2b = by_word_fe2.get(hi_word, [])
                hi_start2 = bisect.bisect_left(hi_fe2b, new_ev.fe1_cycle)
                hi_top2 = bisect.bisect_right(hi_fe2b, rec.fe_cycle)
                for i in range(hi_start2, hi_top2):
                    ev = hi_candidates[i]
                    if ev.fe1_cycle >= new_ev.fe1_cycle:
                        new_hi = ev
                        break
                if new_hi is not None:
                    rec.if1_hi = new_hi.fe1_cycle
                    rec.if2_hi = new_hi.fe2_cycle
                    rec.ic_miss_hi = new_hi.ic_miss
        else:
            # No later event for this record's lo word. The line is
            # still resident from an earlier iteration and the icache
            # state never re-pulsed. Synthesize fetch cycles.
            #
            # Sequential FE timing off the previous record's if1. A matching
            # lo or hi word shares that fetch, anything else takes the next
            # cycle, and fe_cycle is the ceiling minus the pipeline depth.
            depth = 3 if rec.wraps_line else 2
            seq_if1 = None
            if (prev_rec_with_if1 is not None
                    and prev_rec_with_if1.pc is not None
                    and prev_rec_with_if1.if1_lo is not None):
                try:
                    prev_pc_int = int(prev_rec_with_if1.pc, 16)
                    prev_lo_word = prev_pc_int & ~FETCH_OFFSET_MASK
                    prev_hi_word = (prev_lo_word + FETCH_BYTES
                                    if prev_rec_with_if1.wraps_line
                                    else None)
                    if prev_lo_word == lo_word:
                        seq_if1 = prev_rec_with_if1.if1_lo
                    elif (prev_hi_word == lo_word
                          and prev_rec_with_if1.if1_hi is not None):
                        seq_if1 = prev_rec_with_if1.if1_hi
                    else:
                        last_prev_fetch = (prev_rec_with_if1.if1_hi
                                           if prev_rec_with_if1.wraps_line
                                           and prev_rec_with_if1.if1_hi
                                           is not None
                                           else prev_rec_with_if1.if1_lo)
                        seq_if1 = last_prev_fetch + 1
                except (TypeError, ValueError):
                    seq_if1 = None
            if seq_if1 is None:
                # No usable prev context. Anchoring on fe_cycle overstates
                # the FE gap when the issue stage stalled, since the FE
                # fetched on time and the instruction sat in instr_queue.
                seq_if1 = max(prev_if1, rec.fe_cycle - depth)
            synth_if1 = max(seq_if1, prev_if1)
            # Ceiling: synth must leave enough room before fe_cycle
            # for the FE pipeline depth.
            if rec.fe_cycle is not None:
                ceiling = rec.fe_cycle - depth
                if synth_if1 > ceiling:
                    synth_if1 = max(prev_if1, ceiling)
            if rec.wraps_line:
                rec.if1_lo = synth_if1
                rec.if2_lo = synth_if1 + 1
                rec.if1_hi = rec.if2_lo  # shares cycle (pipelined)
                rec.if2_hi = rec.if1_hi + 1
                rec.ic_miss = False
                rec.ic_miss_hi = False
            else:
                rec.if1_lo = synth_if1
                rec.if2_lo = synth_if1 + 1
                rec.ic_miss = False
            n_synth += 1
        prev_if1 = rec.if1_lo
        prev_rec_with_if1 = rec

    # RVC-pair sharing. Two compressed instructions in one fetch word arrive
    # in a single transaction, so the second inherits the first's binding. The
    # main loop is per-record and can otherwise pick different events.
    n_rvc_paired = 0
    for i in range(1, len(records)):
        prev = records[i - 1]
        curr = records[i]
        if prev.pc is None or curr.pc is None:
            continue
        if not (prev.is_compressed and curr.is_compressed):
            continue
        try:
            prev_pc = int(prev.pc, 16)
            curr_pc = int(curr.pc, 16)
        except (TypeError, ValueError):
            continue
        # Same FETCH_BYTES-aligned block and consecutive 2-byte slots. At
        # FETCH_BYTES=4 a pair check suffices, at 8 the iteration chains
        # across a run of up to 4 compressed records in one block.
        if (prev_pc & ~FETCH_OFFSET_MASK) != (curr_pc & ~FETCH_OFFSET_MASK):
            continue
        if curr_pc != prev_pc + 2:
            continue
        if prev.if1_lo is None:
            continue
        if curr.if1_lo == prev.if1_lo:
            continue  # already aligned (the common case after main loop)
        curr.if1_lo = prev.if1_lo
        curr.if2_lo = prev.if2_lo
        curr.ic_miss = prev.ic_miss
        # Hi side too if both records are wraps_line. (Unusual for a
        # compressed pair to wrap, but defensive.)
        if prev.wraps_line and curr.wraps_line:
            curr.if1_hi = prev.if1_hi
            curr.if2_hi = prev.if2_hi
            curr.ic_miss_hi = prev.ic_miss_hi
        n_rvc_paired += 1

    return n_matched, n_unmatched, n_wraps_with_hi, n_rebound, n_synth


def tag_branch_bubbles(records):
    """Attribute each bubble to its causer and recovery, walking records in id
    order for [non-flushed][flushed run][non-flushed]. The causer is classified
    mispred, unpred or flush_other. Returns the kind counts and diagnostics."""
    counts = {"mispred": 0, "unpred": 0, "flush_other": 0, "pred_taken": 0}
    diag = {
        "bp_mispredict_total":         0,
        "bp_mispredict_flushed":       0,
        "bp_mispredict_no_followers":  0,
        "bp_mispredict_end_of_trace":  0,
    }
    if len(records) < 2:
        return counts, diag

    # Defensive sort by id. completed[] is mostly ordered already, but a
    # flushed record is appended at detection time, which can fire on the same
    # cycle as a commit. At about 50k records the cost is negligible.
    ordered = sorted(records, key=lambda r: r.id if r.id is not None else -1)
    n = len(ordered)

    # First pass diagnostic: count total bp_mispredict=True records
    # and how many are flushed. This is a sanity check against Phase
    # 7a's resolved-branch mispredict count.
    for r in ordered:
        if r.bp_mispredict is True:
            diag["bp_mispredict_total"] += 1
            if r.flushed:
                diag["bp_mispredict_flushed"] += 1

    i = 0
    while i < n:
        # Advance to the next non-flushed record. Records at the very
        # start of the trace that are flushed (early kills) have
        # no causer. We skip them silently.
        while i < n and ordered[i].flushed:
            i += 1
        if i >= n:
            break
        causer = ordered[i]
        # Look ahead: count consecutive flushed records after the
        # causer, then find the next non-flushed (the recovery).
        j = i + 1
        while j < n and ordered[j].flushed:
            j += 1
        if j == i + 1:
            # The very next record is also non-flushed → no bubble.
            # If this causer was a mispredicting branch, note it as
            # "instant recovery". No wrong-path fetches happened.
            if causer.bp_mispredict is True:
                diag["bp_mispredict_no_followers"] += 1
            i = j
            continue
        if j >= n:
            # Bubble exists but the trace ended before a recovery
            # record materialized. We don't tag this one (no recovery
            # id to point at).
            if causer.bp_mispredict is True:
                diag["bp_mispredict_end_of_trace"] += 1
            break
        recovery = ordered[j]
        bubble_size = j - i - 1  # count of flushed records between

        # unpred is NoCF, the predictor said nothing. mispred is a guess
        # that was wrong. pred_taken belongs to the second pass, since a
        # correct prediction leaves no flushed run behind it.
        pcf = causer.bp_predicted_cf
        if causer.fu == "CTRL_FLOW" and (pcf is None or pcf == "NoCF"):
            kind = "unpred"
        elif causer.bp_mispredict is True:
            kind = "mispred"
        else:
            kind = "flush_other"
            # Dual-commit CSR partner. A CSR flush takes effect a cycle
            # after commit, so an op that dual-committed beside it looks like
            # the causer. Prefer a CSR that committed in the same cycle.
            if i > 0:
                prev = ordered[i - 1]
                if (not prev.flushed
                        and prev.co_cycle is not None
                        and causer.co_cycle is not None
                        and prev.co_cycle == causer.co_cycle
                        and prev.fu == "CSR"):
                    causer = prev
            # Self-flushed CSR. A committing CSR write asserts flush_ex_o,
            # which re-flushes the CSR itself, leaving it flushed with wb_cycle
            # but no co_cycle. It, not the innocent predecessor, is the cause.
            #
            # fu='CSR' with wb_cycle set and flushed=True, anywhere in the
            # run. Take the first in commit order, and shrink bubble_size by
            # one since the CSR did real work.
            if causer.fu != "CSR":
                for k in range(i + 1, j):
                    cand = ordered[k]
                    if (cand.fu == "CSR"
                            and cand.wb_cycle is not None
                            and cand.flushed):
                        causer = cand
                        bubble_size = (j - i - 1) - 1
                        break

        causer.bubble_kind = kind
        causer.bubble_caused_cycles = bubble_size
        causer.bubble_recovery_id = recovery.id
        recovery.bubble_from_branch_id = causer.id
        recovery.bubble_cycles = bubble_size
        counts[kind] += 1

        # Continue from the recovery, which may itself cause the next bubble,
        # a corrected branch that mispredicts in turn. Leaving i = j lets the
        # outer scan pick it up immediately.
        i = j

    # Second pass. Taken-branch fetch bubbles (no-flush cases).
    # After the flush-based pass above, three categories of taken
    # control flow can still be untagged:
    #
    #   pred_taken: the predictor was right. Every correctly predicted taken
    #               branch still leaves a 1 to 2 cycle FE1 gap, since the
    #               BHT and BTB see the branch at FE2. Always no-flush.
    #
    #   mispred:    the predictor guessed and was wrong. Handled by the
    #               flush-based pass when a flushed run exists. CVA6 also
    #               recovers instantly with no records flushed.
    #
    #   unpred:     the predictor was silent, so not-taken is assumed and a
    #               branch that resolves taken sets mispredict. Same instant
    #               recovery, sometimes with no wrong-path fetches to kill.
    #
    # All three are found by scanning forward to the next non-flushed record
    # with if1_lo set, and a gap over 1 cycle is a bubble. The flush-based pass
    # wins, so causers and recoveries it already tagged are skipped.
    for i in range(n):
        causer = ordered[i]
        if causer.flushed:
            continue
        if causer.if1_lo is None:
            continue
        if causer.bubble_kind is not None:
            continue  # already tagged by flush-based pass

        # Only a branch, jump or return can cause an FE redirect bubble, and
        # fu == 'CTRL_FLOW' is required too: the RVFI predict bus does not
        # self-clear, so a following load can carry a stale prediction.
        if causer.fu != "CTRL_FLOW":
            continue

        # Is this taken control flow? bp_resolved_taken is the authoritative
        # signal and jumps and returns are always taken. A seen prediction is
        # enough when bp_resolved_taken was dropped.
        is_taken = (causer.bp_resolved_taken is True
                    or causer.bp_predicted_cf in ("Jump", "Return"))
        if not is_taken:
            continue

        # Measure from the causer's LAST fetch cycle, if2_hi when it wraps and
        # if2_lo otherwise. The redirect cannot happen before delivery, and
        # measuring from if1 would absorb the causer's own icache stall.
        causer_fetch_end = (causer.if2_hi
                            if (causer.wraps_line
                                and causer.if2_hi is not None)
                            else causer.if2_lo)
        if causer_fetch_end is None:
            continue

        for j in range(i + 1, n):
            nxt = ordered[j]
            if nxt.flushed:
                continue
            if nxt.if1_lo is None:
                continue
            if nxt.bubble_from_branch_id is not None:
                break  # next is already a recovery for another bubble
            delta = nxt.if1_lo - causer_fetch_end
            if delta > 1:
                bubble_size = delta - 1
                # Classify by predictor state, as the flush-based pass does.
                # NoCF means nothing was predicted, so the redirect is unpred
                # whatever bp_mispredict says, JAL bypassing resolution.
                pcf = causer.bp_predicted_cf
                if pcf is None or pcf == "NoCF":
                    kind = "unpred"
                elif causer.bp_mispredict is True:
                    kind = "mispred"
                else:
                    kind = "pred_taken"
                    # CVA6's static decoder fires at FE2, so the FE issues
                    # the target one cycle later and the redirect costs 1.
                    # Beyond that is IQ backpressure, not the predictor.
                    bubble_size = min(bubble_size, 1)
                causer.bubble_kind = kind
                causer.bubble_caused_cycles = bubble_size
                causer.bubble_recovery_id = nxt.id
                nxt.bubble_from_branch_id = causer.id
                nxt.bubble_cycles = bubble_size
                counts[kind] += 1
            break

    return counts, diag


# ============================================================================
# Pipeline tracker
# ============================================================================

class PipelineTracker:
    """Maintains queues of in-flight instances and applies handshake/flush
    events. Order discipline: each queue is strict FIFO (in-order pipeline)."""

    def __init__(self, n_wb_ports=NR_WB_PORTS, n_commit_ports=NR_COMMIT_PORTS):
        self.n_wb_ports = n_wb_ports
        self.n_commit_ports = n_commit_ports


        self.fetched = deque()        # has fe_cycle, awaiting decode
        self.decoded = deque()        # has id_cycle, awaiting issue
        self.issued = {}              # trans_id -> record, awaiting wb/commit
        self.completed = []           # terminal list

        self.next_id = 0
        self.n_committed = 0
        self.n_flushed_if = 0
        self.n_flushed_id = 0
        self.n_flushed_ex = 0
        self.n_unmatched_writebacks = 0
        self.n_unmatched_commits = 0

        # Realigner signal counters.
        #
        # serving_unaligned_o is registered unaligned_q, high while the
        # realigner holds a fetch's upper half. It chains: a fetch that
        # completes one unaligned and begins another has no 0 to 1 edge.
        #
        # Therefore neither counter below equals the wraps_line record
        # count directly. The relationships are:
        #
        #   - n_realigner_unaligned_starts, the 0 to 1 transitions, counts
        #     the unaligned runs the realigner began. A run can yield no
        #     records if kill_s2 catches it, or several when they chain.
        #
        #   - n_realigner_unaligned_cycles, the cycles high, is the stall
        #     time unaligned_q was held. It inflates with icache gaps between
        #     the contributing fetches, so it is a stall metric not a count.
        #
        # wraps_line tagging is verified separately by the 100% lo to hi
        # binding rate in match_records_to_events: every such record finds two
        # distinct I$ events at the expected word addresses.
        self.n_realigner_unaligned_starts = 0
        self.n_realigner_unaligned_cycles = 0

        # Forwarding diagnostics, firing only when a real forward coincides
        # with the producer on the wb bus. All zero means via=wb is impossible
        # here. Nonzero with a zero via=wb stat means the via writer is wrong.
        self._diag_n_issue_cycles = 0
        self._diag_n_issue_with_any_wb = 0
        self._diag_n_real_match_rs1 = 0
        self._diag_n_real_match_rs2 = 0
        self._diag_n_real_match_rs3 = 0

        # I$ event timeline, filled per cycle by the walker. After the walk
        # match_records_to_events binds if1/if2/ic_miss onto each record by
        # 4-byte-aligned PC.
        self.icache_timeline = ICacheTimeline()

        # LSU FSM correlation via lsu_ctrl.trans_id.
        # See on_lsu_fsm_sample for details.
        #
        # An admit-while-busy is pop_ld=1 in SEND_TAG or pop_st=1 in
        # VALID_STORE, so state_q does not transition. pending_admit_*_tid_str
        # defers it a cycle to line up with the FSM's logical handoff.
        self.active_lsu_load = None
        self.active_lsu_store = None
        self.prev_load_state_str = None
        self.prev_store_state_str = None
        self.prev_lsu_ctrl_trans_id_str = None
        self.pending_admit_load_tid_str = None
        self.pending_admit_store_tid_str = None

        # D$ event log, filled by on_dcache_sample during the scan. After it
        # completes, attribute_dc_events_to_records binds events to records by
        # the [admit, complete] window, plus tid where the event carries one.
        #
        # Events arrive in cycle order, since the caller invokes
        # on_dcache_sample once per rising edge ascending. The attribution
        # pass relies on that instead of re-sorting.
        self._dc_events = []
        self._rfsm_active_cycles = set()

        # Dirty victim writeback events, logged in cycle order. Pairing and
        # the AXI-write-latency aggregate happen in finalize_writebacks().
        # Send and ack pair by flush slot id, alloc and ack join by nline.
        self._wb_allocs = []   # (cycle, nline_hex, way_onehot)
        self._wb_sends = []    # (cycle, slot_id_int, addr_hex)
        self._wb_acks = []     # (cycle, slot_id_int, nline_hex)
        # Dirty-victim evictions from the miss handler.
        # (cycle, incoming_nline_hex, victim_way_onehot). Joined to writebacks
        # by (set, way) in finalize_writebacks.
        self._wb_evicts = []
        self.writeback_events = []
        self.writeback_stats = {}

    # -- per-stage event handlers ------------------------------------------

    def on_fetch(self, cycle, pc, instr_word, is_compressed):
        # Mask the instruction word to 16 bits when compressed. The frontend
        # field carries the line's lower 32 bits, which for an RVC pair holds
        # both, and the one at this PC is in the low half.
        if is_compressed and instr_word is not None:
            try:
                instr_word = f"0x{int(instr_word, 16) & 0xFFFF:04x}"
            except ValueError:
                pass
        # wraps_line comes from PC and size: a 32-bit instruction at offset 6
        # of an 8B block has its upper half in the next fetch. Equivalent to
        # serving_unaligned_o asserting at the realigner's output cycle.
        wraps = self._compute_wraps_line(pc, is_compressed)
        rec = InstructionRecord(
            id=self.next_id,
            pc=pc,
            instr_word=instr_word,
            is_compressed=is_compressed,
            fe_cycle=cycle,
            wraps_line=wraps,
        )
        self.fetched.append(rec)
        self.next_id += 1

    @staticmethod
    def _compute_wraps_line(pc, is_compressed):
        """True when the instruction straddles a fetch-block boundary: a 32-bit
        instruction at offset FETCH_BYTES-2 has its upper half in the next
        block (cva6_icache.sv:158, 428), so the realigner combines two fetches."""
        if is_compressed or pc is None:
            return False
        try:
            return (int(pc, 16) & FETCH_OFFSET_MASK) == FETCH_BYTES - 2
        except (TypeError, ValueError):
            return False

    def on_fetch_dropped(self, cycle, pc, instr_word, is_compressed):
        """An FE handshake on a cycle where flush_unissued_instr_i is high.
        id_stage.sv:444 forces issue_n[0].valid=0, so the frontend pops but the
        entry is discarded. Recorded as flushed and kept out of `fetched`."""
        if is_compressed and instr_word is not None:
            try:
                instr_word = f"0x{int(instr_word, 16) & 0xFFFF:04x}"
            except ValueError:
                pass
        wraps = self._compute_wraps_line(pc, is_compressed)
        rec = InstructionRecord(
            id=self.next_id,
            pc=pc,
            instr_word=instr_word,
            is_compressed=is_compressed,
            fe_cycle=cycle,
            flushed=True,
            flush_reason="fetch_dropped_fui",
            wraps_line=wraps,
        )
        self.completed.append(rec)
        self.next_id += 1
        self.n_flushed_if += 1

    def on_decode_issue(self, cycle, trans_id,
                        fu_val=None, rs1=None, rs2=None, rd=None,
                        bp_cf_val=None, bp_predict_target=None,
                        fwd_rs1_used=False, fwd_rs2_used=False, fwd_rs3_used=False,
                        ihz_rs1=None, ihz_rs2=None, ihz_rs3=None,
                        wb_view=None):
        """Combined decode and issue. issue_instr_o is a combinational passthrough
        of decoded_instr_i (scoreboard.sv:151), so both handshakes fire together
        and trans_id must be read from IPTR on that one cycle."""
        if not self.fetched:
            return
        # Forwarding diagnostics, counted only on a real forward. All zero
        # means no forward in the trace coincides with the producer's wb, so
        # via=wb=0 is the true answer for this build.
        self._diag_n_issue_cycles += 1
        wb_tids_set = {tid for _port, tid in (wb_view or [])}
        if wb_view:
            self._diag_n_issue_with_any_wb += 1
            if fwd_rs1_used and ihz_rs1 in wb_tids_set:
                self._diag_n_real_match_rs1 += 1
            if fwd_rs2_used and ihz_rs2 in wb_tids_set:
                self._diag_n_real_match_rs2 += 1
            if fwd_rs3_used and ihz_rs3 in wb_tids_set:
                self._diag_n_real_match_rs3 += 1
        rec = self.fetched.popleft()
        rec.id_cycle = cycle
        rec.is_cycle = cycle
        rec.ex_cycle = cycle + 1
        rec.trans_id = trans_id
        if fu_val is not None:
            rec.fu = FU_NAME.get(fu_val, f"UNK_{fu_val}")
            rec.fu_category = FU_CATEGORY.get(rec.fu, "Other")
        rec.rs1 = rs1
        rec.rs2 = rs2
        rec.rd = rd
        # Branch prediction snapshot. bp_cf_val is the cf_t
        # enum int from mem_q[trans_id].sbe.bp.cf. bp_predict_target
        # is the VLEN-bit predict_address as int (or None).
        if bp_cf_val is not None:
            rec.bp_predicted_cf = CF_T_NAMES.get(
                bp_cf_val, f"UNK_{bp_cf_val}")
            # Only attach a target when a prediction was made, leaving None
            # for NoCF to avoid the misleading 0 target.
            if rec.bp_predicted_cf != "NoCF":
                rec.bp_predicted_target = bp_predict_target
        # Forwarding capture. Where forwarding fired, look the producer
        # trans_id up in the same-cycle wb_view to classify the path as "wb",
        # bypassed off the writeback bus, or "sb", read from the scoreboard.
        wb_tids = {tid for _port, tid in (wb_view or [])}
        if fwd_rs1_used:
            rec.fwd_rs1_used = True
            rec.fwd_rs1_from_tid = ihz_rs1
            rec.fwd_rs1_via = "wb" if ihz_rs1 in wb_tids else "sb"
        if fwd_rs2_used:
            rec.fwd_rs2_used = True
            rec.fwd_rs2_from_tid = ihz_rs2
            rec.fwd_rs2_via = "wb" if ihz_rs2 in wb_tids else "sb"
        if fwd_rs3_used:
            rec.fwd_rs3_used = True
            rec.fwd_rs3_from_tid = ihz_rs3
            rec.fwd_rs3_via = "wb" if ihz_rs3 in wb_tids else "sb"
        self.issued[trans_id] = rec
        # No LSU pending-assignment needed here.
        # Correlation happens via lsu_ctrl.trans_id at FSM-transition
        # time in on_lsu_fsm_sample, looking up self.issued[prev_tid].

    def on_branch_resolved(self, cycle, pc_str, target_str,
                           is_taken_str, is_mispredict_str, cf_type_str):
        """Handle a resolution pulse, high for one cycle when branch_unit resolves
        (branch_unit.sv:84). Bound to an in-flight CTRL_FLOW record by pc,
        oldest is_cycle first. A resolution with no match is dropped."""
        if pc_str is None:
            return
        pc_int = binary_to_int(pc_str)
        if pc_int is None:
            return
        pc_hex = f"0x{pc_int:x}"

        # Find candidate records: in-flight CTRL_FLOW with matching pc.
        candidates = []
        for tid, rec in self.issued.items():
            if rec.fu != "CTRL_FLOW":
                continue
            if rec.pc is None:
                continue
            # rec.pc is the hex string written by on_fetch. Normalize
            # both sides for comparison (some pc values lose leading
            # zeros after binary_to_hex. Compare as ints).
            try:
                rec_pc_int = int(rec.pc, 16)
            except (TypeError, ValueError):
                continue
            if rec_pc_int == pc_int:
                candidates.append((rec.is_cycle or 0, tid, rec))

        if not candidates:
            return

        # Oldest-first if multiple share the PC (loop iteration).
        candidates.sort(key=lambda x: (x[0], x[1]))
        _, _, rec = candidates[0]

        # Refuse to overwrite a prior resolution on the same record,
        # would only happen if the same record were resolved twice,
        # which CVA6's in-order issue precludes.
        if rec.bp_resolution_cycle is not None:
            return

        rec.bp_resolution_cycle = cycle
        rec.bp_resolved_cf = cf_name(cf_type_str)
        rec.bp_resolved_target = binary_to_int(
            target_str) if target_str else None
        rec.bp_resolved_taken = (is_taken_str == "1")
        rec.bp_mispredict = (is_mispredict_str == "1")

        # Derive bp_predicted_cf from the resolution signals. The pre-edge
        # decoded_instr_i.bp.cf misattributes back-to-back issues and mem_q is
        # not always dumped, but branch_unit.sv:99 gives an invertible relation:
        #
        #   is_mispredict = comp_res XOR (predict.cf == Branch)
        #
        # so predict.cf is reconstructable from (resolved_cf, taken,
        # mispredict) for any record that reached resolution. More
        # authoritative than either capture, being the logic HW flushes on.
        resolved = rec.bp_resolved_cf
        taken = rec.bp_resolved_taken
        mis = rec.bp_mispredict
        derived = None
        if resolved == "Branch":
            # Conditional branch. branch_unit overwrites cf_type to Branch on
            # the resolution path, losing the predictor's cf, but the XOR
            # recovers it: predict.cf was Branch iff taken XOR mispredict.
            if taken is not None and mis is not None:
                derived = "Branch" if (taken ^ mis) else "NoCF"
        elif resolved == "Jump":
            # Direct JAL. Frontend.sv:256 unconditionally sets cf=Jump
            # for every JAL. There is no NoCF path for a direct jump
            # the front end identifies as such.
            derived = "Jump"
        elif resolved == "JumpR":
            # JALR. branch_unit.sv:101-107 enters this path only on JALR,
            # and a mispredict means either a BTB miss or a wrong target. The
            # BTB-miss case dominates, so use NoCF on mispredict else JumpR.
            if mis is True:
                derived = "NoCF"
            elif mis is False:
                derived = "JumpR"
        elif resolved == "Return":
            # Returns are predicted by the RAS, so predict.cf was Return
            # either way. No mispredict is a RAS hit, mispredict is a wrong
            # target or an underflow, and only the address was wrong.
            derived = "Return"
        # else leave whatever the pre-edge capture put in. Rare, a record
        # reaching resolution with a resolved_cf outside the four, which
        # resolve_branch's gating should prevent.
        if derived is not None:
            rec.bp_predicted_cf = derived

    def on_lsu_fsm_sample(self, cycle, load_state_str, store_state_str,
                          lsu_ctrl_trans_id_str=None,
                          pop_ld_str=None, pop_st_str=None):
        """Correlate the LSU FSMs. Admissions come from IDLE to non-IDLE, from
        pop_ld/pop_st while the FSM stays in SEND_TAG or VALID_STORE, and from
        leaving those two states, which are mutually exclusive by construction."""

        # ---- LOAD FSM ----
        if load_state_str is not None:
            try:
                new_int = int(load_state_str, 2)
            except (ValueError, TypeError):
                new_int = None
            new_name = LOAD_FSM_NAMES.get(new_int, f"?{load_state_str}")

            if self.prev_load_state_str is None:
                # First sample. Just prime the cache.
                self.prev_load_state_str = load_state_str
            else:
                handled_admit_this_cycle = False

                # Rule B: pending admit from prev-cycle's
                # pop_ld_o=1 / SEND_TAG combination.
                if self.pending_admit_load_tid_str is not None:
                    tid = binary_to_int(self.pending_admit_load_tid_str)
                    rec = self.issued.get(tid) if tid is not None else None
                    # Close out the old active record (handoff).
                    if self.active_lsu_load is not None:
                        self.active_lsu_load.lsu_complete_cycle = cycle
                    if rec is not None:
                        self.active_lsu_load = rec
                        rec.lsu_admit_cycle = cycle
                        if rec.lsu_state_history is None:
                            rec.lsu_state_history = []
                        rec.lsu_state_history.append({
                            "cycle": cycle, "state": new_name})
                    else:
                        self.active_lsu_load = None
                    self.pending_admit_load_tid_str = None
                    handled_admit_this_cycle = True

                # Rule A: state transition admission / completion /
                # mid-flight, only if rule B didn't already handle
                # an admission this cycle.
                if (not handled_admit_this_cycle
                        and load_state_str != self.prev_load_state_str):
                    try:
                        old_int = int(self.prev_load_state_str, 2)
                    except (ValueError, TypeError):
                        old_int = None
                    old_name = LOAD_FSM_NAMES.get(old_int, "?")

                    if old_name == "IDLE" and new_name != "IDLE":
                        # Rule A: standard admission.
                        tid = None
                        if self.prev_lsu_ctrl_trans_id_str is not None:
                            tid = binary_to_int(
                                self.prev_lsu_ctrl_trans_id_str)
                        rec = self.issued.get(tid) if tid is not None else None
                        if rec is not None:
                            self.active_lsu_load = rec
                            rec.lsu_admit_cycle = cycle
                            if rec.lsu_state_history is None:
                                rec.lsu_state_history = []
                            rec.lsu_state_history.append({
                                "cycle": cycle, "state": new_name})
                    elif old_name == "SEND_TAG" and new_name != "IDLE":
                        # Rule B', admit-while-busy: SEND_TAG to any
                        # non-IDLE state. Per load_unit.sv:320-354 only IDLE
                        # and SEND_TAG are not new admissions.
                        if self.active_lsu_load is not None:
                            self.active_lsu_load.lsu_complete_cycle = cycle
                        tid = None
                        if self.prev_lsu_ctrl_trans_id_str is not None:
                            tid = binary_to_int(
                                self.prev_lsu_ctrl_trans_id_str)
                        rec = self.issued.get(tid) if tid is not None else None
                        if rec is not None:
                            self.active_lsu_load = rec
                            rec.lsu_admit_cycle = cycle
                            if rec.lsu_state_history is None:
                                rec.lsu_state_history = []
                            rec.lsu_state_history.append({
                                "cycle": cycle, "state": new_name})
                        else:
                            self.active_lsu_load = None
                    elif new_name == "IDLE":
                        if self.active_lsu_load is not None:
                            self.active_lsu_load.lsu_complete_cycle = cycle
                            self.active_lsu_load = None
                    else:
                        if self.active_lsu_load is not None:
                            rec = self.active_lsu_load
                            if rec.lsu_state_history is None:
                                rec.lsu_state_history = []
                            rec.lsu_state_history.append({
                                "cycle": cycle, "state": new_name})

                self.prev_load_state_str = load_state_str

                # Rule B detect: schedule pending admit for next cycle.
                if (pop_ld_str == "1" and new_name == "SEND_TAG"
                        and lsu_ctrl_trans_id_str is not None):
                    self.pending_admit_load_tid_str = lsu_ctrl_trans_id_str

        # ---- STORE FSM (mirror) ----
        if store_state_str is not None:
            try:
                new_int = int(store_state_str, 2)
            except (ValueError, TypeError):
                new_int = None
            new_name = STORE_FSM_NAMES.get(new_int, f"?{store_state_str}")

            if self.prev_store_state_str is None:
                self.prev_store_state_str = store_state_str
            else:
                handled_admit_this_cycle = False

                if self.pending_admit_store_tid_str is not None:
                    tid = binary_to_int(self.pending_admit_store_tid_str)
                    rec = self.issued.get(tid) if tid is not None else None
                    if self.active_lsu_store is not None:
                        self.active_lsu_store.lsu_complete_cycle = cycle
                    if rec is not None:
                        self.active_lsu_store = rec
                        rec.lsu_admit_cycle = cycle
                        if rec.lsu_state_history is None:
                            rec.lsu_state_history = []
                        rec.lsu_state_history.append({
                            "cycle": cycle, "state": new_name})
                    else:
                        self.active_lsu_store = None
                    self.pending_admit_store_tid_str = None
                    handled_admit_this_cycle = True

                if (not handled_admit_this_cycle
                        and store_state_str != self.prev_store_state_str):
                    try:
                        old_int = int(self.prev_store_state_str, 2)
                    except (ValueError, TypeError):
                        old_int = None
                    old_name = STORE_FSM_NAMES.get(old_int, "?")

                    if old_name == "IDLE" and new_name != "IDLE":
                        # Rule A: standard admission.
                        tid = None
                        if self.prev_lsu_ctrl_trans_id_str is not None:
                            tid = binary_to_int(
                                self.prev_lsu_ctrl_trans_id_str)
                        rec = self.issued.get(tid) if tid is not None else None
                        if rec is not None:
                            self.active_lsu_store = rec
                            rec.lsu_admit_cycle = cycle
                            if rec.lsu_state_history is None:
                                rec.lsu_state_history = []
                            rec.lsu_state_history.append({
                                "cycle": cycle, "state": new_name})
                    elif old_name == "VALID_STORE" and new_name != "IDLE":
                        # Rule B', admit-while-busy out of VALID_STORE. Per
                        # store_unit.sv:179-206 only IDLE and VALID_STORE are
                        # not admissions, leaving the two wait states.
                        if self.active_lsu_store is not None:
                            self.active_lsu_store.lsu_complete_cycle = cycle
                        tid = None
                        if self.prev_lsu_ctrl_trans_id_str is not None:
                            tid = binary_to_int(
                                self.prev_lsu_ctrl_trans_id_str)
                        rec = self.issued.get(tid) if tid is not None else None
                        if rec is not None:
                            self.active_lsu_store = rec
                            rec.lsu_admit_cycle = cycle
                            if rec.lsu_state_history is None:
                                rec.lsu_state_history = []
                            rec.lsu_state_history.append({
                                "cycle": cycle, "state": new_name})
                        else:
                            self.active_lsu_store = None
                    elif new_name == "IDLE":
                        if self.active_lsu_store is not None:
                            self.active_lsu_store.lsu_complete_cycle = cycle
                            self.active_lsu_store = None
                    else:
                        if self.active_lsu_store is not None:
                            rec = self.active_lsu_store
                            if rec.lsu_state_history is None:
                                rec.lsu_state_history = []
                            rec.lsu_state_history.append({
                                "cycle": cycle, "state": new_name})

                self.prev_store_state_str = store_state_str

                if (pop_st_str == "1" and new_name == "VALID_STORE"
                        and lsu_ctrl_trans_id_str is not None):
                    self.pending_admit_store_tid_str = lsu_ctrl_trans_id_str

        # ---- Cache lsu_ctrl.trans_id for next cycle's transitions ----
        if lsu_ctrl_trans_id_str is not None:
            self.prev_lsu_ctrl_trans_id_str = lsu_ctrl_trans_id_str

    def on_dcache_sample(self, cycle,
                         mallo, mtid, msid, mpf, mnline_alloc,
                         mchk, mchk_nline, mchkhit,
                         rfsm, rrsp, rtid):
        """Capture per-cycle HPDcache miss-handler events into `self._dc_events`,
        with refill-FSM activity tracked separately for the overlap test.
        Missing signals are tolerated, their events are simply not generated."""
        # MSHR allocation pulse. The sid separates a load-adapter allocation
        # from a store, CMO or HWPF one, and only LOAD_ADAPTER_SIDS can become
        # a dc_primary_miss. Stored raw so it can be reclassified later.
        if mallo == "1":
            self._dc_events.append({
                "cycle": cycle,
                "type":  "alloc",
                "sid":   binary_to_int(msid) if msid else None,
                "tid":   binary_to_int(mtid) if mtid else None,
                "pf":    binary_to_int(mpf) if mpf else None,
                "nline": binary_to_int(mnline_alloc) if mnline_alloc else None,
            })

        # MSHR-check pulse, 'check_hit' or 'check_miss' from the
        # combinational mshr_check_hit_o on the same edge. check_hit is
        # coalescing. The check path has no sid, so attribution is by cycle.
        if mchk == "1":
            hit = (mchkhit == "1")
            self._dc_events.append({
                "cycle": cycle,
                "type":  "check_hit" if hit else "check_miss",
                "nline": binary_to_int(mchk_nline) if mchk_nline else None,
            })

        # Refill response: when refill data finally reaches the core
        # port for a primary miss. Tid identifies which requestor
        # gets the data (see hpdcache_miss_handler.sv:382,397).
        if rrsp == "1":
            self._dc_events.append({
                "cycle": cycle,
                "type":  "refill_rsp",
                "tid":   binary_to_int(rtid) if rtid else None,
            })

        # Any non-zero refill state means a refill is writing the data RAM,
        # updating the directory or invalidating, consuming the RAM port and
        # stalling unrelated loads. Hence dc_refill_overlap even for hits.
        if rfsm is not None:
            rfsm_val = binary_to_int(rfsm)
            if rfsm_val is not None and rfsm_val != REFILL_FSM_IDLE:
                self._rfsm_active_cycles.add(cycle)

    def attribute_dc_events_to_records(self):
        """Bind D$ events to LOAD and STORE records once the scan is done, copying
        events inside each record's [admit, complete] window and setting
        dc_primary_miss, dc_coalesced and dc_refill_overlap from them."""
        # Index events by cycle so each record binary-searches into its window
        # instead of rescanning from cycle zero, with rfsm_sorted doing the same
        # for refill overlap. The old per-record scan looked like a hang.
        evlog = self._dc_events
        n_events = len(evlog)
        ev_cycles = [ev["cycle"] for ev in evlog]
        rfsm_sorted = sorted(self._rfsm_active_cycles)

        # Positions in evlog already claimed by an earlier store. The store
        # window is extended by HPDCACHE_STORE_LOOKAHEAD, so without this two
        # nearby stores would both count the same alloc.
        consumed_store_alloc_idx = set()

        n_loads = n_stores = 0
        n_prim = n_coal = n_overlap = 0

        for rec in self.completed:
            if rec.fu_category != "Mem":
                continue
            if rec.fu == "LOAD":
                n_loads += 1
            elif rec.fu == "STORE":
                n_stores += 1

            admit = rec.lsu_admit_cycle
            complete = rec.lsu_complete_cycle
            if admit is None or complete is None:
                rec.dc_events = []
                continue

            # Bounded to this record's [admit, window_end] via the bisect
            # below, so the work per record is proportional to the events in
            # its window, not the whole log.
            events_in_window = []
            primary_miss = False
            coalesced = False

            # A store's FSM reaches IDLE when the cache acks, but its MSHR
            # alloc fires several cycles later in st0-st1-st2. Loads keep the
            # plain window, since load_unit waits for the data.
            HPDCACHE_STORE_LOOKAHEAD = 5
            if rec.fu == "STORE":
                window_end = complete + HPDCACHE_STORE_LOOKAHEAD
            else:
                window_end = complete

            start_idx = bisect.bisect_left(ev_cycles, admit)
            for ev_idx in range(start_idx, n_events):
                ev = evlog[ev_idx]
                c = ev["cycle"]
                if c > window_end:
                    break
                events_in_window.append(ev)
                etype = ev["type"]
                if etype == "alloc":
                    sid = ev.get("sid")
                    # Both LSU FSMs are serial, so a sid=1 alloc in a LOAD's
                    # window is that load and sid=3 in a STORE's is that
                    # store. The cache's tid cannot disambiguate.
                    if rec.fu == "LOAD" and sid == LOAD_UNIT_SID:
                        primary_miss = True
                    elif rec.fu == "STORE" and sid == STORE_ADAPTER_SID:
                        # Skip if a previous store already claimed
                        # this alloc. Prevents double-counting when
                        # store windows overlap due to the lookahead.
                        if ev_idx in consumed_store_alloc_idx:
                            continue
                        consumed_store_alloc_idx.add(ev_idx)
                        primary_miss = True
                elif etype == "check_hit":
                    coalesced = True

            # Refill overlap: any cycle in [admit, complete] in the
            # rFSM-active set. Set lookup is O(1) per cycle.
            rf_lo = bisect.bisect_left(rfsm_sorted, admit)
            refill_overlap = (rf_lo < len(rfsm_sorted)
                              and rfsm_sorted[rf_lo] <= complete)

            rec.dc_events = events_in_window

            # check_hit has no source-ID on the miss handler, but load_unit's
            # FSM is single-threaded and holds at most one load in WGT, so a
            # check_hit inside a LOAD's window is almost surely that load's.
            #
            # For STOREs, an sid=3 alloc in their window sets
            # dc_primary_miss, store_unit being serial too. dc_coalesced and
            # dc_refill_overlap stay LOAD-only, stores do not emit check_i.
            if rec.fu == "LOAD":
                rec.dc_primary_miss = primary_miss
                rec.dc_coalesced = coalesced
                rec.dc_refill_overlap = refill_overlap

                if primary_miss:
                    n_prim += 1
                if coalesced:
                    n_coal += 1
                if refill_overlap:
                    n_overlap += 1
            elif rec.fu == "STORE":
                rec.dc_primary_miss = primary_miss
                if primary_miss:
                    n_prim += 1

        # Perf-counter view of misses. evt_cache_read_miss_o counts every
        # non-prefetch MSHR alloc (hpdcache_ctrl_pe.sv:368), where
        # n_primary_miss_loads counts only those attributed to a LOAD.
        n_miss_total = 0
        n_miss_loads_g = 0   # global, sid==LOAD_UNIT_SID, regardless of tid match
        n_miss_stores = 0   # sid==STORE_ADAPTER_SID
        n_miss_other = 0   # PTW, accel, CMO, unknown
        for ev in evlog:
            if ev.get("type") != "alloc":
                continue
            if ev.get("pf") == 1:
                continue
            n_miss_total += 1
            sid = ev.get("sid")
            if sid == LOAD_UNIT_SID:
                n_miss_loads_g += 1
            elif sid == STORE_ADAPTER_SID:
                n_miss_stores += 1
            else:
                n_miss_other += 1

        return {
            "total_dc_events":       n_events,
            "rfsm_active_cycles":    len(self._rfsm_active_cycles),
            "n_loads":               n_loads,
            "n_stores":              n_stores,
            "n_primary_miss_loads":  n_prim,
            "n_coalesced_loads":     n_coal,
            "n_refill_overlap_loads": n_overlap,
            "n_dcache_miss_events_total":  n_miss_total,
            "n_dcache_miss_events_loads":  n_miss_loads_g,
            "n_dcache_miss_events_stores": n_miss_stores,
            "n_dcache_miss_events_other":  n_miss_other,
        }

    # -- Dirty victim writeback (flush/wback unit) ----------------------

    def on_wback_sample(self, cycle,
                        alloc_v, alloc_r, alloc_nline, alloc_way,
                        send_v, send_r, send_id, send_addr,
                        ack_v, ack_r, ack_id, ack_nline):
        """Log writeback handshakes in cycle order: alloc hands the victim to the
        flush unit, send issues the memory write, ack is the response. Pairing
        is deferred to finalize_writebacks()."""
        if alloc_v == "1" and alloc_r == "1":
            self._wb_allocs.append((cycle, binary_to_hex(alloc_nline),
                                    binary_to_int(alloc_way)))
        if send_v == "1" and send_r == "1":
            self._wb_sends.append((cycle, binary_to_int(send_id),
                                   binary_to_hex(send_addr)))
        if ack_v == "1" and ack_r == "1":
            self._wb_acks.append((cycle, binary_to_int(ack_id),
                                  binary_to_hex(ack_nline)))

    def on_evict_sample(self, cycle, alloc_v, wback, mshr_nline, victim_way):
        """Log a dirty-victim eviction when the miss handler allocates with wback.
        The victim shares the set and way with the incoming line, which is how
        finalize_writebacks joins it to its writeback."""
        if alloc_v == "1" and wback == "1":
            self._wb_evicts.append((cycle, binary_to_hex(mshr_nline),
                                    binary_to_int(victim_way)))

    def finalize_writebacks(self):
        """Pair send with ack by flush slot id for the AXI write latency, join
        alloc with ack by nline for total residency, and build the event list
        and latency aggregate."""
        send_q = defaultdict(deque)
        for c, sid, addr in self._wb_sends:
            send_q[sid].append((c, addr))

        events = []
        latencies = []
        unmatched_acks = 0
        for ac, sid, nline in self._wb_acks:     # acks already in cycle order
            if send_q[sid]:
                sc, addr = send_q[sid].popleft()
                lat = ac - sc
                latencies.append(lat)
                events.append({
                    "send_cycle":          sc,
                    "ack_cycle":           ac,
                    "alloc_cycle":         None,   # filled by the nline join
                    "flush_slot":          sid,
                    "nline":               nline,
                    "addr":                addr,
                    "axi_write_latency":   lat,
                    "residency":           None,
                    "way":                 None,   # victim way (one-hot->idx)
                    "evict_incoming_nline": None,   # line X that displaced Y
                    "evict_cycle":         None,
                    "linked":              False,
                })
            else:
                unmatched_acks += 1
        sends_never_acked = sum(len(q) for q in send_q.values())

        # alloc -> ack join by nline (FIFO per nline, ack-time order). Also
        # carries the one-hot victim way captured at flush_alloc.
        alloc_q = defaultdict(deque)
        for c, nline, way in self._wb_allocs:
            alloc_q[nline].append((c, way))
        for ev in events:
            q = alloc_q.get(ev["nline"])
            if q:
                ac_cycle, way_oh = q.popleft()
                ev["alloc_cycle"] = ac_cycle
                ev["residency"] = ev["ack_cycle"] - ac_cycle
                ev["way"] = _onehot_to_idx(way_oh)

        events.sort(key=lambda e: e["send_cycle"])

        # --- eviction linkage: join each writeback to the dirty eviction
        # that caused it, by (set, victim_way) nearest within a small window
        # (validated: same-cycle, delta=0. Window absorbs handshake skew).
        SET_MASK = (1 << 8) - 1          # 256 sets -> setWidth 8
        WINDOW = 4
        n_linked = 0
        # (set, way_oh) -> [(cycle, X_hex), ...]
        ev_by_key = defaultdict(list)
        for ec, x_nline, vway_oh in self._wb_evicts:
            x_int = int(x_nline, 16) if x_nline else None
            s = None if x_int is None else (x_int & SET_MASK)
            ev_by_key[(s, vway_oh)].append((ec, x_nline))
        for v in ev_by_key.values():
            v.sort()
        used = defaultdict(set)
        for ev in events:
            y_int = int(ev["nline"], 16) if ev["nline"] else None
            s = None if y_int is None else (y_int & SET_MASK)
            anchor = ev["alloc_cycle"] if ev["alloc_cycle"] is not None else ev["send_cycle"]
            way_oh = None
            # recover the one-hot from the stored index (single bit)
            if ev["way"] is not None:
                way_oh = 1 << ev["way"]
            cands = ev_by_key.get((s, way_oh), [])
            best = None
            for idx, (ec, x_hex) in enumerate(cands):
                if idx in used[(s, way_oh)]:
                    continue
                if abs(ec - anchor) <= WINDOW:
                    if best is None or abs(ec - anchor) < abs(best[1] - anchor):
                        best = (idx, ec, x_hex)
            if best is not None:
                used[(s, way_oh)].add(best[0])
                ev["evict_cycle"] = best[1]
                ev["evict_incoming_nline"] = best[2]
                ev["linked"] = True
                n_linked += 1

        agg = {}
        if latencies:
            ls = sorted(latencies)
            hist = defaultdict(int)
            for L in latencies:
                hist[L] += 1
            agg = {
                "n":         len(ls),
                "min":       ls[0],
                "median":    int(median(ls)),
                "max":       ls[-1],
                "histogram": {str(k): v for k, v in sorted(hist.items())},
            }

        self.writeback_events = events
        self.writeback_stats = {
            "n_allocs":            len(self._wb_allocs),
            "n_sends":             len(self._wb_sends),
            "n_acks":              len(self._wb_acks),
            "matched_pairs":       len(latencies),
            "acks_no_prior_send":  unmatched_acks,
            "sends_never_acked":   sends_never_acked,
            "n_evictions":         len(self._wb_evicts),
            "n_linked":            n_linked,
            "n_unlinked":          len(events) - n_linked,
            "axi_write_latency":   agg,
        }
        return self.writeback_stats

    def on_writeback(self, cycle, port, trans_id,
                     mq_fu=None, mq_rs1=None, mq_rs2=None, mq_rd=None,
                     mq_bp_cf=None):
        rec = self.issued.get(trans_id)
        if rec is None:
            self.n_unmatched_writebacks += 1
            return
        if rec.wb_cycle is None:
            rec.wb_cycle = cycle
        # Overwrite the decoded fields with the authoritative values from the
        # registered mem_q ring, stable from decode+1 to commit. When mem_q is
        # absent the caller passes mq_* = None and the pre-edge values stay.
        if mq_fu is not None:
            rec.fu = FU_NAME.get(mq_fu, f"UNK_{mq_fu}")
            rec.fu_category = FU_CATEGORY.get(rec.fu, "Unknown")
        if mq_rs1 is not None:
            rec.rs1 = mq_rs1
        if mq_rs2 is not None:
            rec.rs2 = mq_rs2
        if mq_rd is not None:
            rec.rd = mq_rd
        # Same correction for the predictor verdict. The pre-edge
        # decoded_instr_i.bp.cf holds the previous instruction on back-to-back
        # issue, while mem_q[trans_id].sbe.bp.cf is stable through commit.
        if mq_bp_cf is not None:
            rec.bp_predicted_cf = CF_T_NAMES.get(mq_bp_cf, f"UNK_{mq_bp_cf}")

    def on_commit(self, cycle, port, trans_id,
                  mq_fu=None, mq_rs1=None, mq_rs2=None, mq_rd=None,
                  mq_bp_cf=None):
        rec = self.issued.pop(trans_id, None)
        if rec is None:
            self.n_unmatched_commits += 1
            return
        rec.co_cycle = cycle
        # Apply mem_q decoded fields if rec.fu wasn't set at
        # writeback (e.g., NONE-fu instructions auto-validate without going
        # through a writeback port. See scoreboard.sv line 189).
        if mq_fu is not None and rec.fu is None:
            rec.fu = FU_NAME.get(mq_fu, f"UNK_{mq_fu}")
            rec.fu_category = FU_CATEGORY.get(rec.fu, "Unknown")
        if mq_rs1 is not None and rec.rs1 is None:
            rec.rs1 = mq_rs1
        if mq_rs2 is not None and rec.rs2 is None:
            rec.rs2 = mq_rs2
        if mq_rd is not None and rec.rd is None:
            rec.rd = mq_rd
        # Same fallback for bp.cf on no-writeback paths
        # (NONE-fu instructions). The writeback fixup catches most, but
        # NONE-fu ones reach commit without ever going through a wb port.
        if mq_bp_cf is not None and rec.bp_predicted_cf is None:
            rec.bp_predicted_cf = CF_T_NAMES.get(mq_bp_cf, f"UNK_{mq_bp_cf}")
        self.completed.append(rec)
        self.n_committed += 1
    # -- flush handlers ----------------------------------------------------

    def _flush_fetched(self, reason):
        while self.fetched:
            rec = self.fetched.popleft()
            rec.flushed = True
            rec.flush_reason = reason
            self.completed.append(rec)
            self.n_flushed_if += 1

    def _flush_decoded(self, reason):
        while self.decoded:
            rec = self.decoded.popleft()
            rec.flushed = True
            rec.flush_reason = reason
            self.completed.append(rec)
            self.n_flushed_id += 1

    def _flush_issued(self, reason):
        for tid in list(self.issued.keys()):
            rec = self.issued.pop(tid)
            rec.flushed = True
            rec.flush_reason = reason
            self.completed.append(rec)
            self.n_flushed_ex += 1

    def on_flush_if(self, cycle):
        self._flush_fetched("flush_if")

    def on_flush_id(self, cycle):
        # ID flush also affects fetched (cascade up).
        self._flush_fetched("flush_id_cascade_if")
        self._flush_decoded("flush_id")

    def on_flush_ex(self, cycle):
        # EX flush cascades back through ID and IF.
        self._flush_fetched("flush_ex_cascade_if")
        self._flush_decoded("flush_ex_cascade_id")
        self._flush_issued("flush_ex_commit_drain")

    # -- finalization ------------------------------------------------------

    def finalize(self):
        # Anything still in-flight at EOF is incomplete. Mark as flushed.
        if self.fetched or self.decoded or self.issued:
            self._flush_fetched("eof")
            self._flush_decoded("eof")
            self._flush_issued("eof")
        # Restore id-sorted order. Flushes can interleave.
        self.completed.sort(key=lambda r: r.id)


# ============================================================================
# VCD header parsing
# ============================================================================

_BIT_RANGE_RE = re.compile(r"\[\d+:\d+\]$")
_ARRAY_INDEX_RE = re.compile(r"\[(\d+)\]")


def strip_bit_range(path):
    while True:
        new = _BIT_RANGE_RE.sub("", path)
        if new == path:
            return path
        path = new


def parse_var_block(f):
    scope_stack = []
    path_to_id = {}
    id_to_path = {}
    timescale = "unknown"
    for line in f:
        line = line.strip()
        if not line:
            continue
        if line.startswith("$enddefinitions"):
            return path_to_id, id_to_path, timescale
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
            vcd_id = tokens[3]
            sig_name = tokens[4]
            if len(tokens) >= 7 and tokens[5] != "$end":
                sig_name += tokens[5]
            full_path = ".".join(scope_stack + [sig_name])
            path_to_id[full_path] = vcd_id
            id_to_path[vcd_id] = full_path
    return path_to_id, id_to_path, timescale


def match_whitelist(whitelist, path_to_id, scope_prefix):
    by_stripped = defaultdict(list)
    for full_path, vcd_id in path_to_id.items():
        by_stripped[strip_bit_range(full_path)].append((full_path, vcd_id))
    matches = []
    for entry in whitelist:
        target = f"{scope_prefix}.{entry}" if scope_prefix else entry
        hits = by_stripped.get(target, [])
        matches.append({
            "whitelist_path": entry,
            "full_paths": [h[0] for h in hits],
            "vcd_ids": [h[1] for h in hits],
        })
    return matches


# Matches the "mem_q[<N>].sbe.fu" tail of a VCD path, with or without a bit
# range. Probes the build's real scoreboard depth before the whitelist runs,
# so a build with more slots than NR_SB_ENTRIES can be refused.
_MEMQ_SLOT_PROBE_RE = re.compile(r"mem_q\[(\d+)\]\.sbe\.fu(?:\[[\d:]+\])?$")

# Probes for the pre-flight guards in main(). A larger value than the
# compile-time default means silent wrong output, since the high indices are
# not in the whitelist. Smaller is fine, the unused slots stay None.
_DECODED_PORT1_RE = re.compile(
    r"decoded_instr_i\[1\]\.fu(?:\[[\d:]+\])?$")
_COMMIT_PTR_RE = re.compile(
    r"commit_pointer_q\[(\d+)\](?:\[[\d:]+\])?$")
_WB_TRANSID_RE = re.compile(
    r"trans_id_i\[(\d+)\](?:\[[\d:]+\])?$")


def probe_max_scoreboard_slot(path_to_id):
    """Largest N with mem_q[N].sbe.fu in the VCD, or -1 when none are present.
    Scans every signal path, not just whitelisted ones, so it sees slots past
    what the tracer enumerates at compile time."""
    max_n = -1
    for path in path_to_id:
        m = _MEMQ_SLOT_PROBE_RE.search(path)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n


def probe_superscalar(path_to_id):
    """Return True iff the build has SuperscalarEn=1 (NrIssuePorts > 1).
    Detected by the presence of decoded_instr_i[1].fu, which only exists
    when the scoreboard's decoded_instr_i array has more than one entry."""
    for path in path_to_id:
        if _DECODED_PORT1_RE.search(path):
            return True
    return False


def probe_max_commit_port(path_to_id):
    """Find the largest N such that commit_pointer_q[N] exists in the VCD.
    Tells us the build's NrCommitPorts. Returns -1 if none found."""
    max_n = -1
    for path in path_to_id:
        m = _COMMIT_PTR_RE.search(path)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n


def probe_max_wb_port(path_to_id):
    """Find the largest N such that trans_id_i[N] exists in the VCD.
    Tells us the build's NrWbPorts. Returns -1 if none found."""
    max_n = -1
    for path in path_to_id:
        m = _WB_TRANSID_RE.search(path)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n


def build_port_map(full_paths, vcd_ids):
    """For array-indexed multi-element signals, map port (first single-index
    in the path) to its VCD ID."""
    result = {}
    for path, vid in zip(full_paths, vcd_ids):
        indices = _ARRAY_INDEX_RE.findall(path)
        if indices:
            port = int(indices[0])
            result[port] = vid
    return result


# ============================================================================
# Value helpers
# ============================================================================

def get_bit(binary_str, bit_idx):
    """Return bit at position bit_idx counted from the LSB. 0 for missing/x/z."""
    if not binary_str:
        return 0
    s = binary_str.strip()
    if not s:
        return 0
    if "x" in s.lower() or "z" in s.lower():
        return 0
    if len(s) <= bit_idx:
        return 0
    return 1 if s[-(bit_idx + 1)] == "1" else 0


def binary_to_int(s):
    if not s:
        return None
    s = s.strip()
    if any(c in s.lower() for c in "xz"):
        return None
    try:
        return int(s, 2)
    except ValueError:
        return None


def binary_to_hex(s):
    n = binary_to_int(s)
    return None if n is None else f"0x{n:x}"


def _onehot_to_idx(v):
    """One-hot integer -> bit index (way number). None or non-one-hot -> None."""
    if v is None or v <= 0:
        return None
    if v & (v - 1):          # more than one bit set: not clean one-hot
        return None
    return v.bit_length() - 1


# ============================================================================
# Streaming
# ============================================================================

def stream_and_extract(f, matches, args, n_wb_ports, n_commit_ports):
    # Build lookup maps from matches.
    single_id = {}    # whitelist_path → vcd_id (for entries with one match)
    port_maps = {}    # whitelist_path → {port: vcd_id} (for multi-element)
    for m in matches:
        if not m["vcd_ids"]:
            continue
        if len(m["vcd_ids"]) == 1:
            single_id[m["whitelist_path"]] = m["vcd_ids"][0]
        else:
            port_maps[m["whitelist_path"]] = build_port_map(
                m["full_paths"], m["vcd_ids"])

    # Collect all tracked VCD IDs for the body filter.
    tracked = set()
    for vid in single_id.values():
        tracked.add(vid)
    for pm in port_maps.values():
        tracked.update(pm.values())

    state = {}
    tracker = PipelineTracker(
        n_wb_ports=n_wb_ports,
        n_commit_ports=n_commit_ports,
    )

    # Quick aliases for the hot path.
    CLK = single_id.get("clk_i")
    FE_V = single_id.get("id_stage_i.fetch_entry_valid_i")
    FE_R = single_id.get("id_stage_i.fetch_entry_ready_o")
    PC_ID = single_id.get("fetch_entry_if_id[0].address")
    IN_ID = single_id.get("fetch_entry_if_id[0].instruction")
    RVC = single_id.get("id_stage_i.rvfi_is_compressed_o")

    DV = single_id.get("issue_stage_i.i_scoreboard.decoded_instr_valid_i")
    DA = single_id.get("issue_stage_i.i_scoreboard.decoded_instr_ack_o")
    # decoded_instr_i fields sampled at decode handshake.
    DFU = single_id.get("issue_stage_i.i_scoreboard.decoded_instr_i[0].fu")
    DRS1 = single_id.get("issue_stage_i.i_scoreboard.decoded_instr_i[0].rs1")
    DRS2 = single_id.get("issue_stage_i.i_scoreboard.decoded_instr_i[0].rs2")
    DRD = single_id.get("issue_stage_i.i_scoreboard.decoded_instr_i[0].rd")
    # decoded_instr_i.bp.{cf,predict_address} at the decode handshake, taken
    # from the pre-edge snapshot to avoid the same advance-on-rising-edge
    # problem as fu/rs1/rs2/rd.
    DBP_CF = single_id.get(
        "issue_stage_i.i_scoreboard.decoded_instr_i[0].bp.cf")
    DBP_TGT = single_id.get(
        "issue_stage_i.i_scoreboard.decoded_instr_i[0].bp.predict_address")

    # Forwarding signals from issue_read_operands, snapshotted pre-edge like
    # decoded_instr_i.*, since forward_rsX and idx_hzd_rsX are combinational
    # too and advance to the next instruction's view on the edge.
    FWD_RS1 = single_id.get("issue_stage_i.i_issue_read_operands.forward_rs1")
    FWD_RS2 = single_id.get("issue_stage_i.i_issue_read_operands.forward_rs2")
    FWD_RS3 = single_id.get("issue_stage_i.i_issue_read_operands.forward_rs3")
    IHZ_RS1 = single_id.get(
        "issue_stage_i.i_issue_read_operands.idx_hzd_rs1[0]")
    IHZ_RS2 = single_id.get(
        "issue_stage_i.i_issue_read_operands.idx_hzd_rs2[0]")
    IHZ_RS3 = single_id.get(
        "issue_stage_i.i_issue_read_operands.idx_hzd_rs3[0]")
    FWD_AVAILABLE = all(s is not None for s in (
        FWD_RS1, FWD_RS2, FWD_RS3, IHZ_RS1, IHZ_RS2, IHZ_RS3))
    if FWD_AVAILABLE:
        stagelog("issue_read_operands forwarding signals resolved",
              file=sys.stderr)
    else:
        missing = [name for name, sig in [
            ("forward_rs1",   FWD_RS1), ("forward_rs2", FWD_RS2),
            ("forward_rs3",   FWD_RS3), ("idx_hzd_rs1[0]", IHZ_RS1),
            ("idx_hzd_rs2[0]", IHZ_RS2), ("idx_hzd_rs3[0]", IHZ_RS3),
        ] if sig is None]
        stagelog("WARNING: Forwarding signals not resolved. "
              "fwd_rsX_* fields will be left null on all records. "
              "Missing: " + ", ".join(missing), file=sys.stderr)
    IV = single_id.get("issue_stage_i.i_scoreboard.issue_instr_valid_o")
    IA = single_id.get("issue_stage_i.i_scoreboard.issue_ack_i")
    IPTR = single_id.get("issue_stage_i.i_scoreboard.issue_pointer_q")

    WTV = single_id.get("issue_stage_i.i_scoreboard.wt_valid_i")
    # trans_id_i per port: explicit per-port whitelist entries.
    TID_MAP = {}
    for port in range(n_wb_ports):
        vid = single_id.get(f"issue_stage_i.i_scoreboard.trans_id_i[{port}]")
        if vid is not None:
            TID_MAP[port] = vid

    # Per-slot maps for scoreboard's registered mem_q.
    # MEMQ_FU[N] = vcd_id of mem_q[N].sbe.fu (None if slot not exposed).
    NR_SB = NR_SB_ENTRIES
    MEMQ_FU = [None] * NR_SB
    MEMQ_RS1 = [None] * NR_SB
    MEMQ_RS2 = [None] * NR_SB
    MEMQ_RD = [None] * NR_SB
    # Authoritative bp.cf. The pre-edge decoded_instr_i snapshot holds the
    # PREVIOUS instruction's bp.cf on back-to-back issue, while registered
    # mem_q[trans_id].sbe.bp.cf is stable from issue+1 through commit.
    MEMQ_BP_CF = [None] * NR_SB
    memq_resolved = 0
    memq_bp_resolved = 0
    for n in range(NR_SB):
        f_vid = single_id.get(f"issue_stage_i.i_scoreboard.mem_q[{n}].sbe.fu")
        r1_vid = single_id.get(
            f"issue_stage_i.i_scoreboard.mem_q[{n}].sbe.rs1")
        r2_vid = single_id.get(
            f"issue_stage_i.i_scoreboard.mem_q[{n}].sbe.rs2")
        rd_vid = single_id.get(f"issue_stage_i.i_scoreboard.mem_q[{n}].sbe.rd")
        bp_cf_vid = single_id.get(
            f"issue_stage_i.i_scoreboard.mem_q[{n}].sbe.bp.cf")
        MEMQ_FU[n] = f_vid
        MEMQ_RS1[n] = r1_vid
        MEMQ_RS2[n] = r2_vid
        MEMQ_RD[n] = rd_vid
        MEMQ_BP_CF[n] = bp_cf_vid
        if all(v is not None for v in (f_vid, r1_vid, r2_vid, rd_vid)):
            memq_resolved += 1
        if bp_cf_vid is not None:
            memq_bp_resolved += 1

    # Detect the real scoreboard depth from MEMQ_FU presence. A sweep build
    # may be smaller, and without shrinking NR_SB the memq check fails and the
    # off-by-one pre-edge fallback produces wrong FU types throughout.
    detected_nr_sb = 0
    for n in range(NR_SB):
        if MEMQ_FU[n] is not None:
            detected_nr_sb = n + 1
        else:
            break
    if 0 < detected_nr_sb < NR_SB:
        stagelog(f"Scoreboard depth: detected {detected_nr_sb} slots in VCD "
              f"(tracer default NR_SB_ENTRIES={NR_SB}). Adapting NR_SB and "
              f"per-slot arrays. This usually means the build has "
              f"NrScoreboardEntries={detected_nr_sb} (TRANS_ID_BITS="
              f"{(detected_nr_sb - 1).bit_length()}).",
              file=sys.stderr)
        NR_SB = detected_nr_sb
        MEMQ_FU = MEMQ_FU[:NR_SB]
        MEMQ_RS1 = MEMQ_RS1[:NR_SB]
        MEMQ_RS2 = MEMQ_RS2[:NR_SB]
        MEMQ_RD = MEMQ_RD[:NR_SB]
        MEMQ_BP_CF = MEMQ_BP_CF[:NR_SB]
        memq_resolved = sum(1 for v in MEMQ_FU if v is not None)
        memq_bp_resolved = sum(1 for v in MEMQ_BP_CF if v is not None)

    MEMQ_AVAILABLE = (memq_resolved == NR_SB)
    MEMQ_BP_AVAILABLE = (memq_bp_resolved == NR_SB)
    if MEMQ_BP_AVAILABLE:
        stagelog("mem_q[*].sbe.bp.cf resolved. Using authoritative "
              "reads at writeback to correct the pre-edge decoded_instr_i "
              "bp.cf misattribution for back-to-back issues",
              file=sys.stderr)
    else:
        stagelog(f"WARNING: mem_q[*].sbe.bp.cf not resolved "
              f"({memq_bp_resolved}/{NR_SB} slots found). Falling back to "
              f"the pre-edge decoded_instr_i.bp.cf snapshot, which is "
              f"INCORRECT for back-to-back issues (the typical loop case): "
              f"the pre-edge sample reads the PREVIOUS instruction's bp.cf "
              f"because issue_q only flips at the rising edge. Most loop "
              f"branches will appear as predicted_cf=NoCF in the output. "
              f"To fix: ensure your Verilator dump includes mem_q[N].sbe.bp "
              f"for all scoreboard slots.",
              file=sys.stderr)

    # decoded_instr_i[0].bp.{cf,predict_address} availability.
    BP_DECODE_AVAILABLE = (DBP_CF is not None and DBP_TGT is not None)
    if BP_DECODE_AVAILABLE:
        stagelog("decoded_instr_i[0].bp.{cf,predict_address} resolved. "
              "using pre-edge snapshot for prediction capture",
              file=sys.stderr)
    else:
        missing = [name for name, sig in [
            ("decoded_instr_i[0].bp.cf", DBP_CF),
            ("decoded_instr_i[0].bp.predict_address", DBP_TGT),
        ] if sig is None]
        stagelog("WARNING: decoded_instr_i.bp.* not resolved. "
              "bp_predicted_* fields will be left None on all records. "
              "Missing: " + ", ".join(missing), file=sys.stderr)

    if MEMQ_AVAILABLE:
        stagelog(f"mem_q ring buffer: all {NR_SB} slots resolved. Using authoritative reads",
              file=sys.stderr)
    elif memq_resolved > 0:
        stagelog(f"mem_q ring buffer: only {memq_resolved}/{NR_SB} slots resolved. "
              "falling back to decode-time pre-edge capture",
              file=sys.stderr)
        MEMQ_AVAILABLE = False
    else:
        stagelog("mem_q ring buffer: NOT exposed in VCD. Falling back to decode-time pre-edge capture",
              file=sys.stderr)

    CA = single_id.get("commit_stage_i.commit_ack_o")
    CPTR_PORTS = [single_id.get(
        f"issue_stage_i.i_scoreboard.commit_pointer_q[{port}]")
        for port in range(NR_COMMIT_PORTS)]

    FIF = single_id.get("flush_ctrl_if")
    FID = single_id.get("flush_ctrl_id")
    FEX = single_id.get("flush_ctrl_ex")
    # Gate the decode handshake by !flush_unissued_instr_i.
    FUI = single_id.get("issue_stage_i.i_scoreboard.flush_unissued_instr_i")
    if FUI is None:
        stagelog("WARNING: flush_unissued_instr_i not resolved. Phantom-decode "
              "gating will be DISABLED and the +N slot drift may return.",
              file=sys.stderr)

    # I$ lookups for ICacheTimeline.on_cycle. STATE_Q is the controller FSM
    # (cva6_icache.sv:122), dreq_o the frontend-side mirror. Access counters
    # increment on icache_dreq_o.req or any ex_stage data_req.
    IC_REQ = single_id.get("i_frontend.icache_dreq_o.req")
    DC_REQ_PORTS = [single_id.get(
        f"ex_stage_i.dcache_req_ports_o[{p}].data_req")
        for p in range(DCACHE_REQ_PORTS)]
    csr_access_resolved = (IC_REQ is not None
                           and all(s is not None for s in DC_REQ_PORTS))
    if csr_access_resolved:
        port_list = ", ".join(
            f"ex_stage_i.dcache_req_ports_o[{p}].data_req"
            for p in range(DCACHE_REQ_PORTS))
        stagelog("CSR-equivalent access counters enabled "
              f"(icache_dreq_o.req + {port_list})",
              file=sys.stderr)
    else:
        missing = []
        if IC_REQ is None:
            missing.append("icache_dreq_o.req")
        for p, s in enumerate(DC_REQ_PORTS):
            if s is None:
                missing.append(f"ex_stage_i.dcache_req_ports_o[{p}].data_req")
        stagelog(f"WARNING: CSR-equivalent access counters not all resolved. "
              f"viewer will fall back to record-derived access counts. "
              f"Missing: {', '.join(missing)}",
              file=sys.stderr)

    # Per-cycle access-event lists, filled by at_rising_edge when the signal
    # was high at the edge, meaning the elapsed cycle had the request up. The
    # viewer windows them for the CSR-equivalent access count.
    ic_access_cycles = []
    dc_access_cycles = []
    # I$ miss pulse cycles, filled where miss_o was high. len() equals
    # perf_counters.sv event 1, and the viewer windows it like the access
    # lists for a region-scoped figure that tracks the counter.
    icache_miss_cycles = []

    STATE_Q = single_id.get(
        "gen_cache_hpd.i_cache_subsystem.i_cva6_icache.state_q")
    IC_VLD = single_id.get("i_frontend.icache_dreq_i.valid")
    IC_VADDR = single_id.get("i_frontend.icache_dreq_i.vaddr")
    IC_K2 = single_id.get("i_frontend.icache_dreq_o.kill_s2")
    IC_MISS_O = single_id.get(
        "gen_cache_hpd.i_cache_subsystem.i_cva6_icache.miss_o")
    icache_resolved = all(s is not None
                          for s in (STATE_Q, IC_VLD, IC_VADDR, IC_K2))
    if not icache_resolved:
        stagelog("WARNING: I$ signals not all resolved. "
              "if1_lo/if2_lo/if1_hi/if2_hi/ic_miss will be left as None "
              "on every record. Missing: " +
              ", ".join(name for name, s in [
                  ("state_q", STATE_Q),
                  ("dreq_o.valid", IC_VLD),
                  ("dreq_o.vaddr", IC_VADDR),
                  ("dreq_i.kill_s2", IC_K2),
              ] if s is None),
              file=sys.stderr)
    else:
        stagelog("I$ tracking enabled (state_q + frontend dreq mirror)",
              file=sys.stderr)

    # instr_realign output flag for the per-cycle pulse
    # counter. Optional. If absent, wraps_line is still populated
    # from PC, just without the cross-validation counter.
    SVU = single_id.get("i_frontend.i_instr_realign.serving_unaligned_o")
    if SVU is None:
        stagelog("WARNING: serving_unaligned_o not resolved. "
              "wraps_line will still be set per record from PC, but the "
              "realigner-pulse cross-validation count will be 0",
              file=sys.stderr)
    else:
        stagelog("instr_realign tracking enabled "
              "(serving_unaligned_o pulse counter for wraps_line "
              "cross-validation)",
              file=sys.stderr)

    # LSU FSM state register lookups.
    LOAD_STATE = single_id.get("ex_stage_i.lsu_i.i_load_unit.state_q")
    STORE_STATE = single_id.get("ex_stage_i.lsu_i.i_store_unit.state_q")
    # lsu_ctrl.trans_id for FSM admission correlation.
    LSU_CTRL_TID = single_id.get("ex_stage_i.lsu_i.lsu_ctrl.trans_id")
    # pop_ld / pop_st for admit-while-busy detection.
    POP_LD = single_id.get("ex_stage_i.lsu_i.lsu_bypass_i.pop_ld_i")
    POP_ST = single_id.get("ex_stage_i.lsu_i.lsu_bypass_i.pop_st_i")
    lsu_resolved = (LOAD_STATE is not None and STORE_STATE is not None)

    # HPDcache miss-handler signals. The gen_cache_hpd. prefix is mandatory:
    # cva6.sv instantiates three cache variants under different generate
    # branches, the others being gen_cache_std and gen_cache_wt.
    _DC_BASE = ("gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
                "hpdcache_miss_handler_i.")
    DC_MALLO = single_id.get(_DC_BASE + "mshr_alloc_i")
    DC_MTID = single_id.get(_DC_BASE + "mshr_alloc_tid_i")
    DC_MSID = single_id.get(_DC_BASE + "mshr_alloc_sid_i")
    DC_MPF = single_id.get(_DC_BASE + "mshr_alloc_is_prefetch_i")
    DC_MNLINE = single_id.get(_DC_BASE + "mshr_alloc_nline_i")
    DC_MCHK = single_id.get(_DC_BASE + "mshr_check_i")
    DC_MCHKN = single_id.get(_DC_BASE + "mshr_check_nline_i")
    DC_MCHKH = single_id.get(_DC_BASE + "mshr_check_hit_o")
    DC_RFSM = single_id.get(_DC_BASE + "refill_fsm_q")
    DC_RRSP = single_id.get(_DC_BASE + "refill_core_rsp_valid_o")
    DC_RTID = single_id.get(_DC_BASE + "refill_core_rsp_o.tid")
    dcache_resolved = all(s is not None for s in [
        DC_MALLO, DC_MTID, DC_MSID, DC_MPF, DC_MNLINE,
        DC_MCHK, DC_MCHKN, DC_MCHKH, DC_RFSM, DC_RRSP, DC_RTID,
    ])

    # Dirty victim writeback (flush/wback unit) signals, all at
    # the i_hpdcache level. Send/ack are the live flush channel. The wbuf
    # channel is dead in this WB config (gen_no_wbuf).
    _WB_BASE = "gen_cache_hpd.i_cache_subsystem.i_dcache.i_hpdcache."
    WB_ALLOC_V = single_id.get(_WB_BASE + "flush_alloc")
    WB_ALLOC_R = single_id.get(_WB_BASE + "flush_alloc_ready")
    WB_ALLOC_NL = single_id.get(_WB_BASE + "flush_alloc_nline")
    WB_SEND_V = single_id.get(_WB_BASE + "mem_req_write_flush_valid")
    WB_SEND_R = single_id.get(_WB_BASE + "mem_req_write_flush_ready")
    WB_SEND_ID = single_id.get(_WB_BASE + "mem_req_write_flush.mem_req_id")
    WB_SEND_AD = single_id.get(_WB_BASE + "mem_req_write_flush.mem_req_addr")
    WB_ACK_V = single_id.get(_WB_BASE + "mem_resp_write_flush_valid")
    WB_ACK_R = single_id.get(_WB_BASE + "mem_resp_write_flush_ready")
    WB_ACK_ID = single_id.get(_WB_BASE + "mem_resp_write_flush.mem_resp_w_id")
    WB_ACK_NL = single_id.get(_WB_BASE + "flush_ack_nline")
    wback_resolved = all(s is not None for s in [
        WB_ALLOC_V, WB_ALLOC_R, WB_ALLOC_NL,
        WB_SEND_V, WB_SEND_R, WB_SEND_ID, WB_SEND_AD,
        WB_ACK_V, WB_ACK_R, WB_ACK_ID, WB_ACK_NL,
    ])
    # Flush-side victim way + miss-handler eviction signals
    # (mshr_alloc_i / mshr_alloc_nline_i reused from the miss-handler group as
    # DC_MALLO / DC_MNLINE).
    WB_FWAY = single_id.get(_WB_BASE + "flush_alloc_way")
    EV_WBACK = single_id.get(_DC_BASE + "mshr_alloc_wback_i")
    EV_VWAY = single_id.get(_DC_BASE + "mshr_alloc_victim_way_i")
    link_resolved = all(s is not None for s in [
        WB_FWAY, EV_WBACK, EV_VWAY, DC_MALLO, DC_MNLINE,
    ])

    # Branch resolution, the bp_resolve_t fields under
    # issue_stage_i.i_scoreboard.resolved_branch_i. valid pulses for one cycle
    # when branch_unit emits a resolution, the rest carry the payload.
    _RB = "issue_stage_i.i_scoreboard.resolved_branch_i."
    RB_VLD = single_id.get(_RB + "valid")
    RB_PC = single_id.get(_RB + "pc")
    RB_TGT = single_id.get(_RB + "target_address")
    RB_TKN = single_id.get(_RB + "is_taken")
    RB_MISP = single_id.get(_RB + "is_mispredict")
    RB_CFT = single_id.get(_RB + "cf_type")
    bp_resolved = all(s is not None for s in
                      [RB_VLD, RB_PC, RB_TGT, RB_TKN, RB_MISP, RB_CFT])
    if not bp_resolved:
        missing = [name for name, sig in [
            ("resolved_branch_i.valid", RB_VLD),
            ("resolved_branch_i.pc", RB_PC),
            ("resolved_branch_i.target_address", RB_TGT),
            ("resolved_branch_i.is_taken", RB_TKN),
            ("resolved_branch_i.is_mispredict", RB_MISP),
            ("resolved_branch_i.cf_type", RB_CFT),
        ] if sig is None]
        stagelog("WARNING: branch-resolve signals not all "
              "resolved. bp_resolved_* fields will be left None "
              "on all records. Missing: " + ", ".join(missing),
              file=sys.stderr)
    else:
        stagelog("Branch resolution tracking enabled "
              "(resolved_branch_i: valid + pc + target + taken + "
              "mispredict + cf_type)", file=sys.stderr)

    if not lsu_resolved:
        missing = []
        if LOAD_STATE is None:
            missing.append("i_load_unit.state_q")
        if STORE_STATE is None:
            missing.append("i_store_unit.state_q")
        stagelog("WARNING: LSU signals not all resolved. "
              "lsu_state_history will be left as None on every record. "
              "Missing: " + ", ".join(missing), file=sys.stderr)
    else:
        extras = []
        if not LSU_CTRL_TID:
            extras.append("lsu_ctrl.trans_id")
        if not POP_LD:
            extras.append("pop_ld")
        if not POP_ST:
            extras.append("pop_st")
        extras_msg = ("" if not extras
                      else f". Degraded (missing: {', '.join(extras)})")
        stagelog("LSU FSM tracking enabled "
              f"(load_unit.state_q + store_unit.state_q + "
              f"lsu_ctrl.trans_id + pop_ld + pop_st){extras_msg}",
              file=sys.stderr)

    # Announce dcache event tracking status.
    if not dcache_resolved:
        missing = [name for name, sig in [
            ("mshr_alloc_i", DC_MALLO),
            ("mshr_alloc_tid_i", DC_MTID),
            ("mshr_alloc_sid_i", DC_MSID),
            ("mshr_alloc_is_prefetch_i", DC_MPF),
            ("mshr_alloc_nline_i", DC_MNLINE),
            ("mshr_check_i", DC_MCHK),
            ("mshr_check_nline_i", DC_MCHKN),
            ("mshr_check_hit_o", DC_MCHKH),
            ("refill_fsm_q", DC_RFSM),
            ("refill_core_rsp_valid_o", DC_RRSP),
            ("refill_core_rsp_o.tid", DC_RTID),
        ] if sig is None]
        stagelog(f"WARNING: Dcache signals not all resolved. "
              f"dc_* fields will be left at defaults. "
              f"Missing: {', '.join(missing)}", file=sys.stderr)
    else:
        stagelog("D$ event tracking enabled "
              "(mshr_alloc + mshr_check + refill_fsm + refill_rsp)",
              file=sys.stderr)

    # Announce writeback (flush/wback) tracking status.
    if not wback_resolved:
        missing = [name for name, sig in [
            ("flush_alloc", WB_ALLOC_V),
            ("flush_alloc_ready", WB_ALLOC_R),
            ("flush_alloc_nline", WB_ALLOC_NL),
            ("mem_req_write_flush_valid", WB_SEND_V),
            ("mem_req_write_flush_ready", WB_SEND_R),
            ("mem_req_write_flush.mem_req_id", WB_SEND_ID),
            ("mem_req_write_flush.mem_req_addr", WB_SEND_AD),
            ("mem_resp_write_flush_valid", WB_ACK_V),
            ("mem_resp_write_flush_ready", WB_ACK_R),
            ("mem_resp_write_flush.mem_resp_w_id", WB_ACK_ID),
            ("flush_ack_nline", WB_ACK_NL),
        ] if sig is None]
        stagelog("WARNING: Writeback signals not all resolved. "
              "writebacks[] will be empty. Missing: " + ", ".join(missing),
              file=sys.stderr)
    else:
        stagelog("dirty-victim writeback tracking enabled "
              "(flush alloc + mem_req_write_flush + mem_resp_write_flush)",
              file=sys.stderr)
        if link_resolved:
            stagelog("writeback<->eviction linkage enabled "
                  "(mshr_alloc_wback + victim_way + flush_alloc_way, "
                  "join by (set,way))", file=sys.stderr)
        else:
            stagelog("WARNING: Linkage signals not all resolved. "
                  "writebacks will have linked=false. (need "
                  "mshr_alloc_wback_i, mshr_alloc_victim_way_i, "
                  "flush_alloc_way)", file=sys.stderr)

    cycle = -1
    first_ts_seen = False
    clk_at_ts_start = "0"
    prev_flush_if = "0"
    prev_flush_id = "0"
    prev_flush_ex = "0"

    # Pre-edge snapshot of decoded_instr_i, sourced from id_stage's registered
    # issue_q which advances at the decode edge. Verilator dumps post-edge, so
    # a naive read at the rising edge yields the NEXT instruction's fields.
    pre_dfu = None
    pre_drs1 = None
    pre_drs2 = None
    pre_drd = None
    # Pre-edge snapshots of decoded_instr_i[0].bp. Reading them from state at
    # the rising-edge timestamp lands on the next instruction, id_stage's
    # issue_q advancing at the same edge that latches the handshake.
    pre_dbp_cf = None
    pre_dbp_tgt = None
    # Pre-edge snapshot of forwarding signals.
    pre_fwd_rs1 = None
    pre_fwd_rs2 = None
    pre_fwd_rs3 = None
    pre_ihz_rs1 = None
    pre_ihz_rs2 = None
    pre_ihz_rs3 = None
    # Pre-edge snapshot of the writeback bus, for the via=sb/wb call. A
    # 1-cycle FU's wb pulse is gone by the consumer's is_cycle, so only the
    # pre-edge value lands on the cycle the wb override fired.
    pre_wtv = None
    pre_tids = {}     # port -> pre-edge trans_id_i[port] (raw VCD string)

    n_lines = 0
    n_changes = 0
    last_ts = 0
    last_report = 0
    start = time.time()
    # Previous-cycle value of serving_unaligned_o, used by
    # at_rising_edge to detect 0→1 transitions = the count of distinct
    # unaligned-instr attempts.
    last_svu = None
    # Clock period detection. The absolute timestamps of the first two rising
    # edges differ by one period, which lets downstream tools convert cycles
    # to real time with no external knowledge of the clock.
    first_re_ts = None
    clock_period_ts = None

    def at_rising_edge():
        nonlocal cycle, prev_flush_if, prev_flush_id, prev_flush_ex, last_svu
        cycle += 1

        # CSR-equivalent access sampling. perf_counters.sv increments while
        # icache_dreq_o.req or any dcache_req_ports_i[0..2].data_req is high.
        # The pre-edge value is what the synchronous counter sees.
        if csr_access_resolved:
            if state.get(IC_REQ, "0") == "1":
                ic_access_cycles.append(cycle)
            if any(state.get(s, "0") == "1" for s in DC_REQ_PORTS):
                dc_access_cycles.append(cycle)

        # I$ miss, pre-edge miss_o. The counter adds one per high cycle and
        # the FSM asserts it once per accepted cacheable ifill
        # (cva6_icache.sv:301-303), wrong-path fills included.
        if IC_MISS_O is not None and state.get(IC_MISS_O, "0") == "1":
            icache_miss_cycles.append(cycle)

        # No drain step. Correlation is via lsu_ctrl.trans_id at
        # FSM-transition time, see on_lsu_fsm_sample, rather than a deferred
        # pending slot. lsu_ctrl is what the FSM itself sees.

        # 1. Commit, before the flush below and before issue can claim the
        # scoreboard slots it releases.
        if CA is not None:
            ca_bus = state.get(CA, "0")
            for port in range(n_commit_ports):
                if get_bit(ca_bus, port) == 1:
                    ptr_id = CPTR_PORTS[port] if port < len(
                        CPTR_PORTS) else None
                    if ptr_id is not None:
                        tid = binary_to_int(state.get(ptr_id))
                        if tid is not None:
                            mq_fu = mq_rs1 = mq_rs2 = mq_rd = None
                            mq_bp_cf = None
                            if MEMQ_AVAILABLE and 0 <= tid < NR_SB:
                                mq_fu = binary_to_int(state.get(MEMQ_FU[tid]))
                                mq_rs1 = binary_to_int(
                                    state.get(MEMQ_RS1[tid]))
                                mq_rs2 = binary_to_int(
                                    state.get(MEMQ_RS2[tid]))
                                mq_rd = binary_to_int(state.get(MEMQ_RD[tid]))
                            if MEMQ_BP_AVAILABLE and 0 <= tid < NR_SB:
                                mq_bp_cf = binary_to_int(
                                    state.get(MEMQ_BP_CF[tid]))
                            tracker.on_commit(cycle, port, tid,
                                              mq_fu, mq_rs1, mq_rs2, mq_rd,
                                              mq_bp_cf)

        # 2. Flush detection on rising edges of flush_ctrl_*.
        flush_if_now = state.get(FIF, "0") if FIF else "0"
        flush_id_now = state.get(FID, "0") if FID else "0"
        flush_ex_now = state.get(FEX, "0") if FEX else "0"
        # EX cascade covers ID + IF, so check it first.
        if flush_ex_now == "1" and prev_flush_ex == "0":
            tracker.on_flush_ex(cycle)
        elif flush_id_now == "1" and prev_flush_id == "0":
            tracker.on_flush_id(cycle)
        elif flush_if_now == "1" and prev_flush_if == "0":
            tracker.on_flush_if(cycle)
        prev_flush_if, prev_flush_id, prev_flush_ex = (
            flush_if_now, flush_id_now, flush_ex_now)

        # 3. Writeback.
        if WTV is not None:
            wt_bus = state.get(WTV, "0")
            for port in range(n_wb_ports):
                if get_bit(wt_bus, port) == 1:
                    tid_vid = TID_MAP.get(port)
                    if tid_vid is not None:
                        tid = binary_to_int(state.get(tid_vid))
                        if tid is not None:
                            mq_fu = mq_rs1 = mq_rs2 = mq_rd = None
                            mq_bp_cf = None
                            if MEMQ_AVAILABLE and 0 <= tid < NR_SB:
                                mq_fu = binary_to_int(state.get(MEMQ_FU[tid]))
                                mq_rs1 = binary_to_int(
                                    state.get(MEMQ_RS1[tid]))
                                mq_rs2 = binary_to_int(
                                    state.get(MEMQ_RS2[tid]))
                                mq_rd = binary_to_int(state.get(MEMQ_RD[tid]))
                            if MEMQ_BP_AVAILABLE and 0 <= tid < NR_SB:
                                mq_bp_cf = binary_to_int(
                                    state.get(MEMQ_BP_CF[tid]))
                            tracker.on_writeback(cycle, port, tid,
                                                 mq_fu, mq_rs1, mq_rs2, mq_rd,
                                                 mq_bp_cf)

        # 4+5. Combined decode+issue handshake. issue_instr_o is a
        # combinational passthrough of decoded_instr_i (scoreboard.sv:151), so
        # DV/DA and IV/IA fire together and must be read as one event.
        if DV and DA and state.get(DV) == "1" and state.get(DA) == "1":
            flush_unissued = (FUI is not None and state.get(FUI) == "1")
            if not flush_unissued:
                tid = binary_to_int(state.get(IPTR))
                if tid is not None:
                    fu_val = binary_to_int(pre_dfu)
                    rs1 = binary_to_int(pre_drs1)
                    rs2 = binary_to_int(pre_drs2)
                    rd = binary_to_int(pre_drd)
                    # bp.cf and bp.predict_address use the same pre-edge
                    # pattern as fu/rs1/rs2/rd. issue_pointer_q advances on
                    # this edge, so mem_q[tid].sbe.bp is the previous slot.
                    bp_cf_val = (binary_to_int(pre_dbp_cf)
                                 if BP_DECODE_AVAILABLE else None)
                    bp_target = (binary_to_int(pre_dbp_tgt)
                                 if BP_DECODE_AVAILABLE else None)
                    # Forwarding snapshot. forward_rsX is combinational off
                    # the current issue_q[0], so post-edge it reflects the
                    # instruction just issued and pre-edge the one before.
                    if FWD_AVAILABLE:
                        live_fwd_rs1 = state.get(FWD_RS1) if FWD_RS1 else None
                        live_fwd_rs2 = state.get(FWD_RS2) if FWD_RS2 else None
                        live_fwd_rs3 = state.get(FWD_RS3) if FWD_RS3 else None
                        fwd_rs1_bit = (live_fwd_rs1 == "1")
                        fwd_rs2_bit = (live_fwd_rs2 == "1")
                        fwd_rs3_bit = (live_fwd_rs3 == "1")
                        ihz_rs1_v = binary_to_int(
                            state.get(IHZ_RS1)) if IHZ_RS1 else None
                        ihz_rs2_v = binary_to_int(
                            state.get(IHZ_RS2)) if IHZ_RS2 else None
                        ihz_rs3_v = binary_to_int(
                            state.get(IHZ_RS3)) if IHZ_RS3 else None
                    else:
                        fwd_rs1_bit = fwd_rs2_bit = fwd_rs3_bit = False
                        ihz_rs1_v = ihz_rs2_v = ihz_rs3_v = None
                    # wb_view from the pre-edge writeback bus, which is Q's
                    # at-head cycle and when the wb override made Q issuable.
                    # Live state would read post-edge, after P's pulse ended.
                    wb_view = []
                    if WTV is not None and pre_wtv is not None:
                        wt_bits = pre_wtv
                        for port, raw_tid in pre_tids.items():
                            # wt_valid_i is dumped MSB-first. Bit `port`
                            # is at index (len - 1 - port).
                            idx = len(wt_bits) - 1 - port
                            if 0 <= idx < len(wt_bits) and wt_bits[idx] == "1":
                                wb_tid = binary_to_int(raw_tid)
                                if wb_tid is not None:
                                    wb_view.append((port, wb_tid))
                    tracker.on_decode_issue(cycle, tid,
                                            fu_val, rs1, rs2, rd,
                                            bp_cf_val, bp_target,
                                            fwd_rs1_bit, fwd_rs2_bit, fwd_rs3_bit,
                                            ihz_rs1_v, ihz_rs2_v, ihz_rs3_v,
                                            wb_view)

        # 6. Fetch.
        #
        # Gate on flush_unissued_instr_i. With it high at an FE handshake,
        # id_stage.sv:444 forces issue_n[0].valid=0 over the valid=1 set at
        # line 433: the frontend pops but id_stage discards the entry.
        #
        # Pushing every FE handshake to `fetched` would leave a phantom that
        # HW's id_stage never had, putting later pops +1 ahead. These go to
        # on_fetch_dropped, recorded as flushed but not queued.
        if FE_V and FE_R and state.get(FE_V) == "1" and state.get(FE_R) == "1":
            pc = binary_to_hex(state.get(PC_ID))
            instr = binary_to_hex(state.get(IN_ID))
            rvc = (state.get(RVC) == "1") if RVC else False
            flush_active = (FUI is not None and state.get(FUI) == "1")
            if flush_active:
                tracker.on_fetch_dropped(cycle, pc, instr, rvc)
            else:
                tracker.on_fetch(cycle, pc, instr, rvc)

        # 7. Feed the I$ event timeline, independent of the record handlers.
        # It observes the controller FSM and dreq handshake, and
        # match_records_to_events binds the events on afterwards by PC.
        if icache_resolved:
            tracker.icache_timeline.on_cycle(
                cycle,
                state.get(STATE_Q),
                state.get(IC_VLD),
                state.get(IC_VADDR),
                state.get(IC_K2),
            )

        # 7b. Realigner sampling. starts counts unaligned runs, cycles counts
        # the stall cycles unaligned_q was high. wraps_line correctness comes
        # from the lo to hi I$ binding, not from these.
        if SVU is not None:
            curr_svu = state.get(SVU)
            if curr_svu == "1":
                tracker.n_realigner_unaligned_cycles += 1
                if last_svu != "1":
                    tracker.n_realigner_unaligned_starts += 1
            last_svu = curr_svu

        # 8. Sample LSU FSMs. Admissions come from IDL to non-IDL transitions
        # via the previous lsu_ctrl.trans_id, and from pop_ld/pop_st in
        # SEND_TAG or VALID_STORE for the admit-while-busy case.
        if lsu_resolved:
            tracker.on_lsu_fsm_sample(
                cycle,
                state.get(LOAD_STATE),
                state.get(STORE_STATE),
                state.get(LSU_CTRL_TID) if LSU_CTRL_TID else None,
                state.get(POP_LD) if POP_LD else None,
                state.get(POP_ST) if POP_ST else None,
            )

        # 9. Sample HPDcache miss-handler signals, capturing alloc, check and
        # refill_rsp pulses plus rFSM-active cycles into a log keyed by cycle.
        # attribute_dc_events_to_records binds them after the scan.
        if dcache_resolved:
            tracker.on_dcache_sample(
                cycle,
                state.get(DC_MALLO),
                state.get(DC_MTID),
                state.get(DC_MSID),
                state.get(DC_MPF),
                state.get(DC_MNLINE),
                state.get(DC_MCHK),
                state.get(DC_MCHKN),
                state.get(DC_MCHKH),
                state.get(DC_RFSM),
                state.get(DC_RRSP),
                state.get(DC_RTID),
            )

        # 9b. Sample the flush/wback unit handshakes. Logged in
        # cycle order. Pairing + AXI-write-latency aggregate computed in
        # finalize_writebacks() after the walk.
        if wback_resolved:
            tracker.on_wback_sample(
                cycle,
                state.get(WB_ALLOC_V),
                state.get(WB_ALLOC_R),
                state.get(WB_ALLOC_NL),
                state.get(WB_FWAY) if WB_FWAY else None,
                state.get(WB_SEND_V),
                state.get(WB_SEND_R),
                state.get(WB_SEND_ID),
                state.get(WB_SEND_AD),
                state.get(WB_ACK_V),
                state.get(WB_ACK_R),
                state.get(WB_ACK_ID),
                state.get(WB_ACK_NL),
            )

        # 9c. Log dirty-victim evictions (mshr_alloc with
        # wback=1). Joined to writebacks by (set, way) in finalize.
        if link_resolved:
            tracker.on_evict_sample(
                cycle,
                state.get(DC_MALLO),
                state.get(EV_WBACK),
                state.get(DC_MNLINE),
                state.get(EV_VWAY),
            )

        # 10. Branch resolution. resolved_branch_o.valid goes high for one
        # cycle at the branch's ex_cycle, or just after under contention, and
        # on_branch_resolved binds it by PC, oldest in-flight on a tie.
        if bp_resolved and state.get(RB_VLD) == "1":
            tracker.on_branch_resolved(
                cycle,
                state.get(RB_PC),
                state.get(RB_TGT),
                state.get(RB_TKN),
                state.get(RB_MISP),
                state.get(RB_CFT),
            )

    for line in f:
        n_lines += 1
        if _PROG is not None and (n_lines & 0x3FFF) == 0:
            _PROG.update(n_lines, len(tracker.completed))
        line = line.rstrip()
        if not line:
            continue
        c0 = line[0]

        if c0 == "#":
            if first_ts_seen:
                curr_clk = state.get(CLK, "0")
                if clk_at_ts_start == "0" and curr_clk == "1":
                    # Clock period from the first two rising edges. last_ts
                    # is the timestamp before this edge took effect, which is
                    # the exact rising-edge time.
                    if first_re_ts is None:
                        first_re_ts = last_ts
                    elif clock_period_ts is None:
                        clock_period_ts = last_ts - first_re_ts
                    at_rising_edge()
            else:
                first_ts_seen = True
            try:
                last_ts = int(line[1:])
            except ValueError:
                pass
            clk_at_ts_start = state.get(CLK) or "0"
            # Snapshot decoded fields before this timestamp's changes apply.
            # If the next `#` shows a rising edge happened here, at_rising_edge
            # reads these pre-edge values for the handshake.
            if DFU:
                pre_dfu = state.get(DFU)
            if DRS1:
                pre_drs1 = state.get(DRS1)
            if DRS2:
                pre_drs2 = state.get(DRS2)
            if DRD:
                pre_drd = state.get(DRD)
            # Pre-edge snapshot of decoded_instr_i[0].bp.
            if DBP_CF:
                pre_dbp_cf = state.get(DBP_CF)
            if DBP_TGT:
                pre_dbp_tgt = state.get(DBP_TGT)
            # Pre-edge snapshot of the forwarding signals, same
            # advance-on-rising-edge concern as decoded_instr_i.*. Post-edge
            # they show the next issue candidate's hazard view.
            if FWD_RS1:
                pre_fwd_rs1 = state.get(FWD_RS1)
            if FWD_RS2:
                pre_fwd_rs2 = state.get(FWD_RS2)
            if FWD_RS3:
                pre_fwd_rs3 = state.get(FWD_RS3)
            if IHZ_RS1:
                pre_ihz_rs1 = state.get(IHZ_RS1)
            if IHZ_RS2:
                pre_ihz_rs2 = state.get(IHZ_RS2)
            if IHZ_RS3:
                pre_ihz_rs3 = state.get(IHZ_RS3)
            # Pre-edge snapshot of the writeback bus. A 1-cycle FU's wb
            # pulse is gone by the consumer's is_cycle, so only the pre-edge
            # read lands on the cycle the wb override fired.
            if WTV:
                pre_wtv = state.get(WTV)
            if TID_MAP:
                pre_tids = {port: state.get(vid)
                            for port, vid in TID_MAP.items()}

            if n_lines - last_report >= 10_000_000:
                elapsed = time.time() - start
                stagelog(
                    f"  ... {n_lines:>15,} lines | "
                    f"{n_changes:>15,} changes | "
                    f"cycle {cycle:>10,} | "
                    f"fetched={tracker.next_id:>8,} "
                    f"committed={tracker.n_committed:>8,} | "
                    f"{elapsed:6.1f}s",
                    file=sys.stderr,
                )
                last_report = n_lines
            continue

        if c0 in "01xXzZ":
            value = c0
            vcd_id = line[1:]
        elif c0 in "bBrR":
            sp = line.find(" ")
            if sp <= 0:
                continue
            value = line[1:sp]
            vcd_id = line[sp + 1:]
        else:
            continue
        n_changes += 1

        if vcd_id in tracked:
            state[vcd_id] = value

    # EOF: flush whatever the final timestamp contained.
    if first_ts_seen:
        curr_clk = state.get(CLK, "0")
        if clk_at_ts_start == "0" and curr_clk == "1":
            at_rising_edge()

    tracker.finalize()

    # Bind I$ events onto records by 4-byte-aligned PC, giving each if1_lo,
    # if2_lo and ic_miss for its first fetch, plus if1_hi and if2_hi when it
    # wraps. No match leaves them None, usually a kill before delivery.
    n_ic_events = len(tracker.icache_timeline.events)
    n_ic_hits = sum(1 for ev in tracker.icache_timeline.events
                    if not ev.ic_miss)
    n_ic_misses = n_ic_events - n_ic_hits
    n_matched, n_unmatched, n_wraps_with_hi, n_rebound, n_synth = match_records_to_events(
        tracker.completed, tracker.icache_timeline.events)
    extra = []
    if n_rebound:
        extra.append(f"{n_rebound} rebound")
    if n_synth:
        extra.append(f"{n_synth} synthesized (cached, no fresh event)")
    extra_str = (", " + ", ".join(extra)) if extra else ""
    stagelog(f"{n_ic_events} I$ events "
          f"({n_ic_hits} hits, {n_ic_misses} misses). "
          f"{n_matched} records matched, {n_unmatched} unmatched"
          + extra_str,
          file=sys.stderr)
    # wraps_line summary. Compare PC-determinative count to
    # the realigner-signal pulse counter for cross-validation. The two
    # should agree up to flushed-mid-realignment edge cases.
    n_wraps = sum(1 for r in tracker.completed if r.wraps_line)
    n_wraps_committed = sum(1 for r in tracker.completed
                            if r.wraps_line and not r.flushed)
    if tracker.n_realigner_unaligned_starts:
        records_per_run = (
            f"records/run = "
            f"{n_wraps / tracker.n_realigner_unaligned_starts:.2f}")
    else:
        records_per_run = "records/run = N/A"
    stagelog(f"wraps_line records = {n_wraps} total "
          f"({n_wraps_committed} committed, "
          f"{n_wraps - n_wraps_committed} flushed). "
          f"{n_wraps_with_hi} bound second fetch (if1_hi/if2_hi). "
          f"Realigner: {tracker.n_realigner_unaligned_starts} runs "
          f"(0→1 transitions), {tracker.n_realigner_unaligned_cycles} "
          f"stall cycles. {records_per_run}.",
          file=sys.stderr)

    # Attribute bubbles to causer and recovery. Walks completed[] in id order
    # for [non-flushed][flushed run][non-flushed], classifies the causer and
    # tags both ends. A CSR that causes no flushed run is not tagged.
    bubble_counts, bubble_diag = tag_branch_bubbles(tracker.completed)
    n_bub_total = sum(bubble_counts.values())
    n_bub_flushed_total = sum(r.bubble_caused_cycles or 0
                              for r in tracker.completed
                              if r.bubble_caused_cycles)
    stagelog(f"Branch bubbles. "
          f"mispred={bubble_counts['mispred']}, "
          f"unpred={bubble_counts['unpred']}, "
          f"flush_other={bubble_counts['flush_other']}, "
          f"pred_taken={bubble_counts['pred_taken']} "
          f"({n_bub_total} causers, {n_bub_flushed_total} total "
          f"wrong-path records flushed).",
          file=sys.stderr)
    # Diagnostic: how bp_mispredict breaks down against the bubble classes.
    # total = flushed + classified + no_followers + end_of_trace + unaccounted,
    # and unaccounted must be 0. A tripwire for records falling through.
    classified = bubble_counts["mispred"] + bubble_counts["unpred"]
    unaccounted = (bubble_diag["bp_mispredict_total"]
                   - bubble_diag["bp_mispredict_flushed"]
                   - classified
                   - bubble_diag["bp_mispredict_no_followers"]
                   - bubble_diag["bp_mispredict_end_of_trace"])
    stagelog(f"Bubble diag: {bubble_diag['bp_mispredict_total']} records "
          f"have bp_mispredict=True "
          f"({bubble_diag['bp_mispredict_flushed']} flushed, "
          f"{classified} tagged as causers, "
          f"{bubble_diag['bp_mispredict_no_followers']} had no "
          f"flushed followers, "
          f"{bubble_diag['bp_mispredict_end_of_trace']} were end-of-trace, "
          f"{unaccounted} unaccounted).",
          file=sys.stderr)

    # Count LOAD/STORE records that got an FSM trace, meaning at least one
    # entry in lsu_state_history. A record whose FSM never moved while pending
    # ends up untraced, which needs an immediate flush and is very rare.
    n_load_traced = 0
    n_load_untraced = 0
    n_store_traced = 0
    n_store_untraced = 0
    for rec in tracker.completed:
        if rec.fu == "LOAD":
            if rec.lsu_state_history:
                n_load_traced += 1
            else:
                n_load_untraced += 1
        elif rec.fu == "STORE":
            if rec.lsu_state_history:
                n_store_traced += 1
            else:
                n_store_untraced += 1
    stagelog(f"LSU FSM traces. "
          f"loads {n_load_traced} traced / {n_load_untraced} untraced. "
          f"stores {n_store_traced} traced / {n_store_untraced} untraced",
          file=sys.stderr)

    # Attribute D$ events to records.
    if dcache_resolved:
        dc_stats = tracker.attribute_dc_events_to_records()
        stagelog(
            f"D$ events. {dc_stats['total_dc_events']} total "
            f"alloc/check/refill_rsp pulses. "
            f"{dc_stats['rfsm_active_cycles']} refill-active cycles. "
            f"Per-record summary: "
            f"{dc_stats['n_primary_miss_loads']} primary-miss / "
            f"{dc_stats['n_coalesced_loads']} coalesced / "
            f"{dc_stats['n_refill_overlap_loads']} refill-overlap "
            f"(of {dc_stats['n_loads']} LOAD + "
            f"{dc_stats['n_stores']} STORE)",
            file=sys.stderr)
        # Perf-counter miss breakdown, the total non-prefetch MSHR allocation
        # count split by allocating adapter. The store adapter usually
        # dominates, loads coalescing onto pending store misses.
        stagelog(
            f"D$ miss events (perf counter view). "
            f"{dc_stats['n_dcache_miss_events_total']} total non-prefetch allocs "
            f"({dc_stats['n_dcache_miss_events_loads']} from LOAD adapter, "
            f"{dc_stats['n_dcache_miss_events_stores']} from STORE adapter, "
            f"{dc_stats['n_dcache_miss_events_other']} from PTW/accel/CMO)",
            file=sys.stderr)
    else:
        dc_stats = {}

    # Branch prediction stats over the completed records. A branch is any
    # record with fu=CTRL_FLOW, predicted means bp_predicted_cf is set and not
    # 'NoCF', resolved means bp_resolution_cycle is set.
    n_cf = 0
    n_pred = 0
    n_resolved = 0
    n_misp = 0
    n_pred_by_cf = {"Branch": 0, "Jump": 0, "JumpR": 0, "Return": 0}
    n_misp_by_cf = {"Branch": 0, "Jump": 0, "JumpR": 0, "Return": 0, "NoCF": 0}
    n_misp_flushed_before_resolve = 0
    for r in tracker.completed:
        if r.fu != "CTRL_FLOW":
            continue
        n_cf += 1
        if r.bp_predicted_cf and r.bp_predicted_cf != "NoCF":
            n_pred += 1
            n_pred_by_cf[r.bp_predicted_cf] = (
                n_pred_by_cf.get(r.bp_predicted_cf, 0) + 1)
        if r.bp_resolution_cycle is not None:
            n_resolved += 1
            if r.bp_mispredict:
                n_misp += 1
                key = r.bp_resolved_cf or "NoCF"
                n_misp_by_cf[key] = n_misp_by_cf.get(key, 0) + 1
        elif r.flush_reason == "flush_ex_branch_mispredict":
            # The branch itself was flushed before our scan saw the
            # resolution pulse. Rare but possible if the resolution
            # cycle coincides with the flush handshake.
            n_misp_flushed_before_resolve += 1
    bp_hit_rate = (
        100.0 * (n_resolved - n_misp) / n_resolved if n_resolved else 0.0)
    stagelog(
        f"Branches. {n_cf} CTRL_FLOW records. "
        f"{n_pred} got a non-NoCF prediction "
        f"({n_pred_by_cf}). "
        f"{n_resolved} reached resolution. "
        f"{n_misp} mispredicts ({n_misp_by_cf}). "
        f"hit rate {bp_hit_rate:.1f}%"
        + (f". {n_misp_flushed_before_resolve} flushed before resolve"
           if n_misp_flushed_before_resolve else ""),
        file=sys.stderr)
    bp_stats = {
        "n_ctrl_flow_records":  n_cf,
        "n_predictions":        n_pred,
        "n_resolutions":        n_resolved,
        "n_mispredicts":        n_misp,
        "n_predictions_by_cf":  n_pred_by_cf,
        "n_mispredicts_by_cf":  n_misp_by_cf,
        "hit_rate_pct":         round(bp_hit_rate, 2),
        "n_flushed_before_resolve": n_misp_flushed_before_resolve,
    }

    # Pair writeback send<->ack, build event list + latency agg.
    if wback_resolved:
        wb_stats = tracker.finalize_writebacks()
        awl = wb_stats.get("axi_write_latency", {})
        stagelog(
            f"Writebacks. {wb_stats['n_allocs']} alloc / "
            f"{wb_stats['n_sends']} send / {wb_stats['n_acks']} ack. "
            f"{wb_stats['matched_pairs']} paired "
            f"({wb_stats['acks_no_prior_send']} acks w/o send, "
            f"{wb_stats['sends_never_acked']} sends unacked). "
            f"AXI write latency: "
            f"min={awl.get('min')} median={awl.get('median')} "
            f"max={awl.get('max')} cyc",
            file=sys.stderr)
        stagelog(
            f"writeback<->eviction linkage. "
            f"{wb_stats.get('n_evictions', 0)} eviction samples. "
            f"{wb_stats.get('n_linked', 0)} writebacks linked / "
            f"{wb_stats.get('n_unlinked', 0)} unlinked",
            file=sys.stderr)
    else:
        wb_stats = {}

    # Forwarding summary across committed records.
    n_fwd_any = 0
    n_fwd_rs1 = n_fwd_rs2 = n_fwd_rs3 = 0
    n_via_sb = n_via_wb = 0
    for r in tracker.completed:
        if r.flushed:
            continue
        used = False
        for via in (r.fwd_rs1_via, r.fwd_rs2_via, r.fwd_rs3_via):
            if via == "sb":
                n_via_sb += 1
                used = True
            elif via == "wb":
                n_via_wb += 1
                used = True
        if r.fwd_rs1_used:
            n_fwd_rs1 += 1
        if r.fwd_rs2_used:
            n_fwd_rs2 += 1
        if r.fwd_rs3_used:
            n_fwd_rs3 += 1
        if used:
            n_fwd_any += 1
    n_committed_seen = sum(1 for r in tracker.completed if not r.flushed)
    fwd_stats = {
        "n_committed_seen":   n_committed_seen,
        "n_with_any_forward": n_fwd_any,
        "n_rs1_forwarded":    n_fwd_rs1,
        "n_rs2_forwarded":    n_fwd_rs2,
        "n_rs3_forwarded":    n_fwd_rs3,
        "n_via_sb":           n_via_sb,
        "n_via_wb":           n_via_wb,
        # How many real forwards had the producer slot on the wb bus in the
        # same cycle, per source. Ground truth for whether via=wb should ever
        # fire.
        "n_issue_cycles":             tracker._diag_n_issue_cycles,
        "n_issue_cycles_with_any_wb": tracker._diag_n_issue_with_any_wb,
        "n_real_match_rs1":           tracker._diag_n_real_match_rs1,
        "n_real_match_rs2":           tracker._diag_n_real_match_rs2,
        "n_real_match_rs3":           tracker._diag_n_real_match_rs3,
    }
    if n_committed_seen:
        pct = 100.0 * n_fwd_any / n_committed_seen
        stagelog(
            f"Forwarding - {n_fwd_any}/{n_committed_seen} "
            f"committed records ({pct:.1f}%) used at least one forwarded "
            f"operand. Rs1={n_fwd_rs1} rs2={n_fwd_rs2} rs3={n_fwd_rs3}. "
            f"via sb/wb = {n_via_sb}/{n_via_wb}",
            file=sys.stderr)
        # Diagnostic: count real forwards (fwd_rsX_used=True) where the
        # producer slot was also on the wb bus this same cycle. These
        # are the cases where via=wb SHOULD fire.
        n_real_match_total = (
            tracker._diag_n_real_match_rs1
            + tracker._diag_n_real_match_rs2
            + tracker._diag_n_real_match_rs3)
        stagelog(
            f"Forwarding diag: {tracker._diag_n_issue_cycles} issue cycles, "
            f"{tracker._diag_n_issue_with_any_wb} had any wt_valid_i bit set. "
            f"Real forward AND producer on wb bus: "
            f"rs1={tracker._diag_n_real_match_rs1} "
            f"rs2={tracker._diag_n_real_match_rs2} "
            f"rs3={tracker._diag_n_real_match_rs3} "
            f"(total={n_real_match_total}).",
            file=sys.stderr)

    stats = {
        "n_lines": n_lines,
        "n_changes": n_changes,
        "last_ts": last_ts,
        "n_cycles": cycle + 1,
        # Scoreboard depth from the mem_q[N].sbe.fu presence scan. Smaller
        # than NR_SB_ENTRIES on a parameterised-down build. Consumers should
        # prefer this over the compile-time default.
        "detected_nr_sb_entries": NR_SB,
        "icache_event_count": n_ic_events,
        "icache_event_hits": n_ic_hits,
        "icache_event_misses": n_ic_misses,
        "icache_records_matched": n_matched,
        "icache_records_unmatched": n_unmatched,
        "lsu_load_records_traced": n_load_traced,
        "lsu_load_records_untraced": n_load_untraced,
        "lsu_store_records_traced": n_store_traced,
        "lsu_store_records_untraced": n_store_untraced,
        "dcache": dc_stats,
        "branch_pred": bp_stats,
        "writeback": wb_stats,
        "forwarding": fwd_stats,
        # Clock period in VCD timescale units, picoseconds for the
        # cva6_testharness sims. Derived from the first two rising edges, None
        # if fewer than two were seen.
        "clock_period_ts": clock_period_ts,
        "first_rising_edge_ts": first_re_ts,
        # CSR-equivalent access cycle lists, one entry per cycle the request
        # signal was high. The viewer counts entries in [cMin, cMax] to match
        # the hardware counters. Empty when the signals were not resolved.
        "ic_access_cycles": ic_access_cycles,
        "dc_access_cycles": dc_access_cycles,
        # I$ miss pulse cycles. len() matches perf_counters.sv event 1, and
        # the viewer windows it like the access lists for a figure that tracks
        # the hardware counter, wrong-path fills included.
        "icache_miss_cycles": icache_miss_cycles,
        "icache_miss_pulses": len(icache_miss_cycles),
    }
    return tracker, stats


# ============================================================================
# Disassembly listing
# ============================================================================
#
# The disassembly pass binds a readable string onto each InstructionRecord
# from an objdump -dS listing passed in with --disasm-list, which keeps any
# toolchain dependency out of the tracer's own environment.
#
# The listing format we parse (from riscv64-unknown-elf-objdump -dS):
#
#     _start:                                       <- function label
#     0000000080003000 <main>:                      <- address-tagged label
#         80003000:<tab>715d                 <tab>addi<tab>sp,sp,-80
#
# The regex below requires leading whitespace on the PC line, so the 64-bit
# address labels at column 0 never match. Source lines, labels, headers and
# directives all start outside [0-9a-f], so they never match either.

_DISASM_LINE_RE = re.compile(
    r'^\s+([0-9a-fA-F]+):\s+([0-9a-fA-F]+)\s+(.+)$'
)


def parse_disasm_list(path):
    """Parse an objdump listing into a PC to string map, whitespace collapsed.
    Anything that does not look like an instruction is ignored. Raises
    FileNotFoundError when the path does not exist."""
    disasm = {}
    with open(path) as f:
        for line in f:
            m = _DISASM_LINE_RE.match(line.rstrip('\n'))
            if not m:
                continue
            pc = int(m.group(1), 16)
            # m.group(3) is everything after the raw bytes: mnemonic +
            # operands + any objdump-resolved symbolic comment. Collapse
            # tabs/spaces into a single space and strip.
            text = re.sub(r'\s+', ' ', m.group(3)).strip()
            disasm[pc] = text
    return disasm


def apply_disasm(records, disasm_map):
    """Annotate each record's `disasm` by PC lookup. Returns (n_annotated,
    n_no_pc, n_unmapped), the last counting PCs outside the listing such as
    bootrom code that is not part of the user program."""
    n_annotated = 0
    n_no_pc = 0
    n_unmapped = 0
    for rec in records:
        if rec.pc is None:
            n_no_pc += 1
            continue
        try:
            pc_int = int(rec.pc, 16)
        except (TypeError, ValueError):
            n_no_pc += 1
            continue
        text = disasm_map.get(pc_int)
        if text is None:
            n_unmapped += 1
        else:
            rec.disasm = text
            n_annotated += 1
    return n_annotated, n_no_pc, n_unmapped


# ============================================================================
# Output
# ============================================================================

CV64A6_HPDC_WB_DEFAULTS = {
    # Mirrors the module-level Config block, which is the single source of
    # truth for the whitelist and lookups. Cache geometry stays literal, it
    # only feeds the viewer's config panel and gates no tracer logic.
    "SuperscalarEn":       SUPERSCALAR_EN,
    "RVC":                 RVC_EN,
    "CvxifEn":             True,
    "NrIssuePorts":        NR_ISSUE_PORTS,
    "NrCommitPorts":       NR_COMMIT_PORTS,
    "NrWbPorts":           NR_WB_PORTS,
    "NrScoreboardEntries": NR_SB_ENTRIES,
    "TRANS_ID_BITS":       TRANS_ID_BITS,
    "FETCH_WIDTH":         FETCH_WIDTH,
    "INSTR_PER_FETCH":     INSTR_PER_FETCH,
    # I-cache geometry. cv64a6_imafdc_sv39_hpdcache_wb canonical values:
    # 16 KiB total, 4-way, 128-bit lines, 256 sets.
    "ICACHE_LINE_WIDTH":   128,
    "ICACHE_SET_ASSOC":    4,
    "ICACHE_NUM_SETS":     256,
    # D-cache geometry: 32 KiB, 8-way, 128-bit lines, 256 sets.
    "DCACHE_LINE_WIDTH":   128,
    "DCACHE_SET_ASSOC":    8,
    "DCACHE_NUM_SETS":     256,
}


# The CV64A6_HPDC_WB_DEFAULTS keys emitted to config_params. The rest are
# tracer assumptions rather than measurements, and stay out so the panel
# cannot contradict a sweep build that varied them.
#
# NrScoreboardEntries is auto-detected from the mem_q[N] enumeration and
# TRANS_ID_BITS derived from it. The rest are verified by the trace working
# at all, since a different value makes the whitelist fail.
#
# To add more verified fields: probe the VCD (Tier 1) or argue
# structural verification (Tier 2), and add the key here.
VERIFIED_CONFIG_FIELDS = frozenset({
    # Tier 1, auto-detected from the VCD.
    "NrScoreboardEntries",
    "TRANS_ID_BITS",
    # Auto-detected from the probed commit_pointer_q and trans_id_i indices,
    # largest seen plus one. A smaller build leaves the high ports out of the
    # dump, so these follow the build rather than the compile-time maxima.
    "NrCommitPorts",
    "NrWbPorts",
    "FETCH_WIDTH",
    "INSTR_PER_FETCH",
})


def write_output_json(output_path, args, stats, tracker):
    metadata = {
        "config_name": "cv64a6_imafdc_sv39_hpdcache_wb",
        "elf_path": None,
        "disasm_list_path": stats.get("disasm_list_path"),
        "vcd_path": str(args.vcd_path),
        "tohost_cycle": None,
        "vcd_scope_prefix": args.scope_prefix,
        "invariants_verified": [],
        # Time base. clock_period_ts is the cycle duration in VCD timescale
        # units and timescale_unit is the VCD's $timescale, '1ps' for the CVA6
        # sims. Together they let the viewer convert cycles to real time.
        "clock_period_ts": stats.get("clock_period_ts"),
        "timescale_unit": stats.get("timescale_unit"),
        "stats": {
            "n_committed": tracker.n_committed,
            "n_flushed_if": tracker.n_flushed_if,
            "n_flushed_id": tracker.n_flushed_id,
            "n_flushed_ex": tracker.n_flushed_ex,
            "n_unmatched_writebacks": tracker.n_unmatched_writebacks,
            "n_unmatched_commits": tracker.n_unmatched_commits,
            # I$ event counts and record-match results.
            "icache_event_count": stats.get("icache_event_count", 0),
            "icache_event_hits": stats.get("icache_event_hits", 0),
            "icache_event_misses": stats.get("icache_event_misses", 0),
            # len(icache_miss_cycles) is miss_o high cycles, which is
            # perf_counters.sv event 1. Unlike icache_event_misses it includes
            # wrong-path fills squashed before delivery.
            "icache_miss_pulses": stats.get("icache_miss_pulses", 0),
            "icache_records_matched": stats.get(
                "icache_records_matched", 0),
            "icache_records_unmatched": stats.get(
                "icache_records_unmatched", 0),
            # Disassembly coverage.
            "disasm_annotated": stats.get("disasm_annotated", 0),
            "disasm_unmapped": stats.get("disasm_unmapped", 0),
            "disasm_no_pc": stats.get("disasm_no_pc", 0),
            # LSU FSM tracking coverage.
            "lsu_load_records_traced": stats.get(
                "lsu_load_records_traced", 0),
            "lsu_store_records_traced": stats.get(
                "lsu_store_records_traced", 0),
            "lsu_load_records_untraced": stats.get(
                "lsu_load_records_untraced", 0),
            "lsu_store_records_untraced": stats.get(
                "lsu_store_records_untraced", 0),
            # D$ event attribution coverage.
            "dcache": stats.get("dcache", {}),
            # Branch prediction tracking coverage.
            "branch_pred": stats.get("branch_pred", {}),
            # Dirty victim writeback + AXI write latency.
            "writeback": stats.get("writeback", {}),
            # Forwarding aggregates.
            "forwarding": stats.get("forwarding", {}),
        },
    }
    # Build config_params: start from the compile-time defaults, apply the
    # runtime-detected overrides, then filter to VERIFIED_CONFIG_FIELDS so the
    # panel cannot contradict the build.
    config_params = dict(CV64A6_HPDC_WB_DEFAULTS)
    detected_sb = stats.get("detected_nr_sb_entries")
    if detected_sb is not None:
        config_params["NrScoreboardEntries"] = detected_sb
        config_params["TRANS_ID_BITS"] = (detected_sb - 1).bit_length()
    detected_cp = stats.get("detected_nr_commit_ports")
    if detected_cp is not None:
        config_params["NrCommitPorts"] = detected_cp
    detected_wb = stats.get("detected_nr_wb_ports")
    if detected_wb is not None:
        config_params["NrWbPorts"] = detected_wb
    config_params = {k: v for k, v in config_params.items()
                     if k in VERIFIED_CONFIG_FIELDS}
    with output_path.open("w") as f:
        f.write("{\n")
        f.write(f'  "metadata": {json.dumps(metadata, indent=2)},\n')
        f.write(f'  "config_params": {json.dumps(config_params, indent=2)},\n')
        f.write(f'  "buffer_maxima": {json.dumps({})},\n')
        f.write('  "instructions": [\n')
        recs = tracker.completed
        for i, rec in enumerate(recs):
            d = asdict(rec)
            comma = "," if i < len(recs) - 1 else ""
            f.write(f"    {json.dumps(d)}{comma}\n")
        f.write("  ],\n")
        # Dirty victim writeback events (separate track, not
        # per-instruction. A writeback is per-evicted-line, many stores
        # coalesce into one line, decoupled in time from the stores).
        wbs = tracker.writeback_events
        f.write('  "writebacks": [\n')
        for i, wb in enumerate(wbs):
            comma = "," if i < len(wbs) - 1 else ""
            f.write(f"    {json.dumps(wb)}{comma}\n")
        f.write("  ],\n")
        # Dcache MSHR allocations as (cycle, sid, pf), so the viewer can
        # compute the perf-counter miss count for any window including PTW,
        # accel and CMO. Only allocs count, check_hit and refill_rsp do not.
        allocs = [ev for ev in tracker._dc_events if ev.get("type") == "alloc"]
        f.write('  "dcache_alloc_events": [\n')
        for i, ev in enumerate(allocs):
            comma = "," if i < len(allocs) - 1 else ""
            row = {"cycle": ev["cycle"], "sid": ev.get(
                "sid"), "pf": ev.get("pf", 0)}
            f.write(f"    {json.dumps(row)}{comma}\n")
        f.write("  ],\n")
        # Icache events as a flat (fe1, fe2, ic_miss) array, so the viewer can
        # compute window-filtered access and miss counts from the FSM signal
        # without any record-derived dedup ambiguity.
        ic_events = tracker.icache_timeline.events
        f.write('  "icache_events": [\n')
        for i, ev in enumerate(ic_events):
            comma = "," if i < len(ic_events) - 1 else ""
            row = {"fe1": ev.fe1_cycle, "fe2": ev.fe2_cycle, "miss": ev.ic_miss}
            f.write(f"    {json.dumps(row)}{comma}\n")
        f.write("  ],\n")
        # Cycles where each request signal was high: icache_dreq_o.req, and
        # any of the three core ports' data_req. Filtering by window gives
        # counts matching mhpmevent 16/17 (perf_counters.sv:126-128).
        ic_acc = stats.get("ic_access_cycles") or []
        dc_acc = stats.get("dc_access_cycles") or []
        ic_miss_cyc = stats.get("icache_miss_cycles") or []
        f.write('  "ic_access_cycles": ' + json.dumps(ic_acc) + ',\n')
        f.write('  "dc_access_cycles": ' + json.dumps(dc_acc) + ',\n')
        f.write('  "icache_miss_cycles": ' + json.dumps(ic_miss_cyc) + '\n')
        f.write("}\n")


# ============================================================================
# Diagnostics
# ============================================================================

def report_missing(matches, path_to_id):
    missing = [m for m in matches if not m["vcd_ids"]]
    if not missing:
        return []
    print(file=sys.stderr)
    print("Missing whitelist entries:", file=sys.stderr)
    for m in missing:
        last_seg = m["whitelist_path"].rsplit(".", 1)[-1]
        # drop array index suffix for search
        last_seg = last_seg.split("[")[0]
        print(f"  - {m['whitelist_path']}", file=sys.stderr)
        cands = [p for p in path_to_id if last_seg in p]
        for c in cands[:5]:
            print(f"      candidate: {c}", file=sys.stderr)
        if len(cands) > 5:
            print(f"      ... And {len(cands) - 5} more", file=sys.stderr)
        if not cands:
            print(
                f"      (no VCD path contains '{last_seg}')", file=sys.stderr)
    return [m["whitelist_path"] for m in missing]


# ============================================================================
# Main
# ============================================================================

def main():
    global _SHOW_STAGES, _PROG
    parser = argparse.ArgumentParser(
        description="Extracts per-instruction pipeline data from a CVA6 "
                    "Verilator VCD and emits JSON for the CVA6Flow viewer.",
    )
    parser.add_argument("vcd_path", help="Path to the .vcd file.")
    parser.add_argument(
        "--scope-prefix",
        default="TOP.ariane_testharness.i_ariane.i_cva6",
        help="Hierarchical prefix to prepend to each whitelist entry.",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output JSON path. Defaults to <vcd_basename>.json.",
    )
    parser.add_argument(
        "--disasm-list",
        default=None,
        help="Path to an objdump -dS listing of the test ELF. When provided, "
             "each record's `disasm` field is populated by PC lookup. "
             "Records whose PC falls outside the listing (e.g. Bootrom) "
             "keep disasm=None.",
    )
    parser.add_argument(
        "--stages", action="store_true",
        help="Show the per-stage resolution diagnostics on stderr (verbose).",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress the streaming progress indicator.",
    )
    args = parser.parse_args()
    _SHOW_STAGES = args.stages

    vcd_path = Path(args.vcd_path)
    if not vcd_path.exists():
        sys.exit(f"VCD file not found: {vcd_path}")
    args.vcd_path = vcd_path

    out_path = Path(
        args.output) if args.output else vcd_path.with_suffix(".json")

    n_wb_ports = CV64A6_HPDC_WB_DEFAULTS["NrWbPorts"]
    n_commit_ports = CV64A6_HPDC_WB_DEFAULTS["NrCommitPorts"]

    file_size = vcd_path.stat().st_size
    print(f"[INFO] Reading {vcd_path} ({file_size / (1024 ** 3):.3f} GB)", file=sys.stderr)
    start = time.time()

    with vcd_path.open("r", errors="replace") as f:
        path_to_id, _id_to_path, timescale = parse_var_block(f)
        print(
            f"[INFO] Header: {len(path_to_id):,} signals, timescale={timescale}", file=sys.stderr)

        # Refuse a VCD whose scoreboard is larger than the compile-time max.
        # The smaller case is auto-handled by shrinking NR_SB, but the
        # whitelist only enumerates slots 0..NR_SB_ENTRIES-1.
        max_sb_slot = probe_max_scoreboard_slot(path_to_id)
        if max_sb_slot >= NR_SB_ENTRIES:
            actual_depth = max_sb_slot + 1
            # NrScoreboardEntries is enforced to be a power of 2 by
            # scoreboard.sv:323, but round up defensively.
            next_pow2 = 1 << (
                actual_depth - 1).bit_length() if actual_depth > 1 else 1
            print(file=sys.stderr)
            print(f"ERROR: VCD contains mem_q[{max_sb_slot}].sbe.fu, implying "
                  f"the build has NrScoreboardEntries >= {actual_depth}, but "
                  f"this tracer was compiled with NR_SB_ENTRIES="
                  f"{NR_SB_ENTRIES}. The whitelist only enumerates slots "
                  f"0..{NR_SB_ENTRIES-1}, so transactions assigned to slots "
                  f"{NR_SB_ENTRIES}..{actual_depth-1} would silently go "
                  f"untracked. The output JSON would have missing writebacks, "
                  f"displaced FU types, and incorrect branch prediction.",
                  file=sys.stderr)
            print(f"To fix: edit NR_SB_ENTRIES near the top of this file "
                  f"(currently {NR_SB_ENTRIES}) to at least {next_pow2}, then "
                  f"rerun. Must be a power of 2.",
                  file=sys.stderr)
            print("Aborting.", file=sys.stderr)
            return 2

        # Refuse superscalar builds. The handshake, IPTR tracking and
        # allocation all assume one instruction per cycle, and wraps_line
        # would use FETCH_BYTES=4 against 8-byte blocks.
        if probe_superscalar(path_to_id):
            print(file=sys.stderr)
            print("ERROR: VCD contains decoded_instr_i[1].fu, implying the "
                  "build has SuperscalarEn=1 (NrIssuePorts > 1). This tracer "
                  "is hardcoded for single-issue and would silently produce "
                  "wrong output (port-1 instructions dropped, fetched/decoded "
                  "queues drifting, wraps_line predicate using FETCH_BYTES=4 "
                  "against 8-byte fetch blocks).",
                  file=sys.stderr)
            print("To fix: superscalar support is non-trivial. Required "
                  "changes include iterating decoded_instr_i[0..NrIssuePorts-1]"
                  " in the WHITELIST and decode+issue handler, reading "
                  "multiple trans_ids per cycle, and updating FETCH_WIDTH + "
                  "the wraps_line predicate for 64-bit fetches.",
                  file=sys.stderr)
            print("Aborting.", file=sys.stderr)
            return 2

        # Refuse builds with more commit ports than the whitelist enumerates.
        # Commits on the high ports would fall off the radar, leaving records
        # that never commit. Smaller builds are fine.
        max_cp = probe_max_commit_port(path_to_id)
        if max_cp >= NR_COMMIT_PORTS:
            actual = max_cp + 1
            print(file=sys.stderr)
            print(f"ERROR: VCD contains commit_pointer_q[{max_cp}], implying "
                  f"the build has NrCommitPorts >= {actual}, but this tracer "
                  f"was compiled with NR_COMMIT_PORTS={NR_COMMIT_PORTS}. The "
                  f"whitelist only enumerates ports 0..{NR_COMMIT_PORTS-1}, "
                  f"so commits on ports {NR_COMMIT_PORTS}..{actual-1} would "
                  f"silently go untracked.",
                  file=sys.stderr)
            print(f"To fix: edit NR_COMMIT_PORTS near the top of this file "
                  f"(currently {NR_COMMIT_PORTS}) to {actual} and rerun.",
                  file=sys.stderr)
            print("Aborting.", file=sys.stderr)
            return 2

        # Pre-flight: refuse builds with more writeback ports than the
        # tracer enumerates. Same logic as the commit-port check, applied
        # to trans_id_i[0..NR_WB_PORTS-1]. Smaller builds work transparently.
        max_wb = probe_max_wb_port(path_to_id)
        if max_wb >= NR_WB_PORTS:
            actual = max_wb + 1
            print(file=sys.stderr)
            print(f"ERROR: VCD contains trans_id_i[{max_wb}], implying the "
                  f"build has NrWbPorts >= {actual}, but this tracer was "
                  f"compiled with NR_WB_PORTS={NR_WB_PORTS}. The whitelist "
                  f"only enumerates ports 0..{NR_WB_PORTS-1}, so writebacks "
                  f"on ports {NR_WB_PORTS}..{actual-1} would silently go "
                  f"untracked, leaving records orphaned in flight.",
                  file=sys.stderr)
            print(f"To fix: edit NR_WB_PORTS near the top of this file "
                  f"(currently {NR_WB_PORTS}) to {actual} and rerun.",
                  file=sys.stderr)
            print("Aborting.", file=sys.stderr)
            return 2

        matches = match_whitelist(WHITELIST, path_to_id, args.scope_prefix)
        missing_paths = report_missing(matches, path_to_id)

        found = {m["whitelist_path"] for m in matches if m["vcd_ids"]}
        missing_required = REQUIRED_SIGNALS - found
        if missing_required:
            print()
            for s in sorted(missing_required):
                print(
                    f"ERROR: required signal '{s}' not found.", file=sys.stderr)
            print("Aborting, the trace cannot be walked.", file=sys.stderr)
            return 2

        tracked = sum(len(m["vcd_ids"]) for m in matches)
        print(f"[INFO] Tracking {tracked} signal IDs across "
              f"{len(matches) - len(missing_paths)}/{len(matches)} whitelist groups", file=sys.stderr)
        print("[parse] streaming VCD body\u2026", file=sys.stderr)
        _PROG = Progress('parse', enabled=not args.quiet)
        tracker, stats = stream_and_extract(
            f, matches, args, n_wb_ports, n_commit_ports)
        # Make the parsed timescale available downstream for the
        # output writer (which builds metadata outside this `with`
        # block and doesn't otherwise see timescale).
        stats["timescale_unit"] = timescale
        # Report the probed commit and writeback port counts rather than the
        # compile-time maxima, since a smaller build leaves the high ports out
        # of the VCD. Probe returns -1 when absent, leaving the default.
        if max_cp >= 0:
            stats["detected_nr_commit_ports"] = max_cp + 1
        if max_wb >= 0:
            stats["detected_nr_wb_ports"] = max_wb + 1

    if _PROG is not None:
        _PROG.done()
    elapsed = time.time() - start
    # The derived clock period, for sanity-checking the time base. Detection
    # uses the first two rising edges and can catch a reset artifact, so the
    # pretty-print is gated on plausibility. The viewer hardcodes 50 MHz.
    cp_ts = stats.get("clock_period_ts")
    if cp_ts is not None and cp_ts >= 1000:  # >= 1 ns, plausible cycle
        ts_unit = stats.get("timescale_unit", "1ps")
        # Best-effort conversion of the parsed timescale string into
        # picoseconds for human-readable output. Anything we don't
        # recognize prints as raw timescale units.
        unit_to_ps = {"1fs": 1e-3, "1ps": 1, "1ns": 1e3,
                      "1us": 1e6, "1ms": 1e9, "1s": 1e12}
        ps = cp_ts * unit_to_ps.get(ts_unit.strip(), 1)
        if ps >= 1e6:
            period_disp = f"{ps/1e6:.3f} us"
        elif ps >= 1e3:
            period_disp = f"{ps/1e3:.3f} ns"
        else:
            period_disp = f"{ps:.0f} ps"
        freq_disp = (f"{1e6/ps:.3f} MHz" if ps > 0 else "?")
        print(f"[INFO] clock period {period_disp} ({freq_disp}), "
              f"timescale {ts_unit}", file=sys.stderr)
    elif cp_ts is not None:
        # Implausibly short, almost certainly first-edge detection tripping on
        # a reset-time sub-cycle event. Do not print the bogus frequency, just
        # note that the viewer falls back to its hardcoded clock.
        print(f"[INFO] clock period: detected {cp_ts} VCD ticks (implausibly "
              f"short. First-edge detection tripped on a sub-cycle "
              f"artifact). Viewer will use its hardcoded 50 MHz.",
              file=sys.stderr)
    else:
        print("[INFO] clock period: could not determine "
              "(need at least 2 rising edges)", file=sys.stderr)

    # CSR-equivalent access totals over the whole trace, which should match
    # mhpmevent 16 and 17 exactly. Empty when the underlying signals were not
    # dumped.
    ic_acc_total = len(stats.get("ic_access_cycles") or [])
    dc_acc_total = len(stats.get("dc_access_cycles") or [])
    if ic_acc_total or dc_acc_total:
        print(f"[INFO] CSR-equivalent accesses (whole trace): "
              f"I-cache={ic_acc_total:,}  D-cache={dc_acc_total:,}",
              file=sys.stderr)
    ic_miss_total = len(stats.get("icache_miss_cycles") or [])
    if ic_miss_total:
        print(f"[INFO] RTL-counter I-cache misses (miss_o pulses, whole "
              f"trace): {ic_miss_total:,}", file=sys.stderr)

    # Annotate records with disassembly text, if a listing was
    # provided. Done after the walk completes so we annotate exactly the
    # records that will be serialized (committed + flushed).
    if args.disasm_list:
        disasm_path = Path(args.disasm_list)
        if not disasm_path.exists():
            print(f"WARNING: --disasm-list {disasm_path} not found. "
                  "skipping disasm annotation.", file=sys.stderr)
            stats["disasm_annotated"] = 0
            stats["disasm_unmapped"] = 0
            stats["disasm_no_pc"] = 0
            stats["disasm_list_path"] = None
        else:
            disasm_map = parse_disasm_list(disasm_path)
            n_ann, n_no_pc, n_unmapped = apply_disasm(
                tracker.completed, disasm_map)
            stagelog(f"Parsed {len(disasm_map):,} disasm entries from "
                  f"{disasm_path.name}. Annotated {n_ann:,} records "
                  f"({n_unmapped:,} unmapped, {n_no_pc:,} without PC)",
                  file=sys.stderr)
            stats["disasm_annotated"] = n_ann
            stats["disasm_unmapped"] = n_unmapped
            stats["disasm_no_pc"] = n_no_pc
            stats["disasm_list_path"] = str(disasm_path)
    else:
        stats["disasm_annotated"] = 0
        stats["disasm_unmapped"] = 0
        stats["disasm_no_pc"] = 0
        stats["disasm_list_path"] = None

    if len(tracker.completed) == 0:
        print("[WARNING] No CVA6 instructions were parsed from this VCD. It may "
              "not be a valid CVA6 Verilator VCD, or the expected pipeline and "
              "commit signals were not found in it. Check that the VCD was "
              "generated from a CVA6 simulation with the RVFI and scoreboard "
              "signals dumped.", file=sys.stderr)

    print(f"[write] writing JSON to {out_path}\u2026", file=sys.stderr)
    write_output_json(out_path, args, stats, tracker)

    mb = file_size / (1024 ** 2)
    speed = mb / elapsed if elapsed > 0 else 0.0

    print()
    print("=" * 78)
    print(" CVA6 Tracer. Summary")
    print("=" * 78)
    print(f" Input                 : {vcd_path}")
    print(f" Output                : {out_path}")
    print(
        f" File size             : {file_size:>15,} bytes ({file_size / (1024**3):.3f} GB)")
    print(f" Lines processed       : {stats['n_lines']:>15,}")
    print(f" Value changes seen    : {stats['n_changes']:>15,}")
    print(f" Cycles seen (rising)  : {stats['n_cycles']:>15,}")
    print(f" Final timestamp       : {stats['last_ts']:>15,}")
    print(f" Elapsed               : {elapsed:>14.1f}s ({speed:.1f} MB/s)")
    print()
    n_total = len(tracker.completed)
    n_compr = sum(1 for r in tracker.completed if r.is_compressed)
    n_flushed = sum(1 for r in tracker.completed if r.flushed)
    print(f" Records total         : {n_total:>15,}")
    print(f"   committed           : {tracker.n_committed:>15,}")
    print(f"   flushed             : {n_flushed:>15,}  "
          f"(IF={tracker.n_flushed_if}, ID={tracker.n_flushed_id}, EX={tracker.n_flushed_ex})")
    print(f"   compressed (RVC)    : {n_compr:>15,}")
    print()
    if tracker.n_unmatched_writebacks:
        print(f" UNMATCHED writebacks  : {tracker.n_unmatched_writebacks}  "
              f"(possible signal/timing issue)")
    if tracker.n_unmatched_commits:
        print(f" UNMATCHED commits     : {tracker.n_unmatched_commits}  "
              f"(possible signal/timing issue)")

    # Disasm coverage summary.
    if args.disasm_list:
        print()
        print(f" Disassembly listing   : {args.disasm_list}")
        print(
            f"   annotated records   : {stats.get('disasm_annotated', 0):>15,}")
        print(
            f"   unmapped (no entry) : {stats.get('disasm_unmapped', 0):>15,}")
        if stats.get('disasm_no_pc', 0):
            print(f"   without PC          : {stats['disasm_no_pc']:>15,}")

    if n_total:
        first_user = next(iter(tracker.completed), None)
        if first_user:
            print()
            print(f" First record:")
            print(f"   id={first_user.id}, pc={first_user.pc}, "
                  f"instr={first_user.instr_word}, compressed={first_user.is_compressed}")
            if first_user.disasm:
                print(f"   disasm={first_user.disasm}")
            print(f"   fu={first_user.fu}, fu_category={first_user.fu_category}, "
                  f"rs1=x{first_user.rs1}, rs2=x{first_user.rs2}, rd=x{first_user.rd}")
            print(f"   fe={first_user.fe_cycle}  id={first_user.id_cycle}  "
                  f"is={first_user.is_cycle}  ex={first_user.ex_cycle}  "
                  f"wb={first_user.wb_cycle}  co={first_user.co_cycle}")
            print(
                f"   trans_id={first_user.trans_id}, flushed={first_user.flushed}")

        # FU / FU-category distribution over committed records.
        cat_user = Counter()
        fu_user = Counter()
        for r in tracker.completed:
            if r.flushed or r.fu_category is None:
                continue
            cat_user[r.fu_category] += 1
            fu_user[r.fu] += 1
        print()
        print(" FU category. Committed records")
        for c in sorted(cat_user):
            print(f"   {c:<10} {cat_user[c]:>8}")
        if fu_user:
            print()
            print(" FU breakdown. Committed records")
            for fu, n in fu_user.most_common():
                print(f"   {fu:<12} {n:>5}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
