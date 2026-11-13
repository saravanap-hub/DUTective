sim_runner.py  (REUSABLE)
Writes the cocotb Makefile + entry file for any DUT, runs the sim, and
classifies the result:
  kind = "pass"      everything ok
  kind = "tb_error"  compile error, crash, timeout, or run ended too early
  kind = "mismatch"  testbench ran fully and the scoreboard reported mismatches
"""
import re
import shutil
import subprocess
from pathlib import Path

MAKEFILE = """TOPLEVEL_LANG = verilog
SIM ?= icarus
VERILOG_SOURCES = {sources}
TOPLEVEL = {top}
COCOTB_TEST_MODULES = test_generated

include $(shell cocotb-config --makefiles)/Makefile.sim
"""

TEST_ENTRY = """import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer
import pyuvm
import generated_tb


@cocotb.test()
async def run_generated(dut):
    generated_tb.DUT = dut
{clock_line}    await Timer(1, unit="ns")
    await pyuvm.uvm_root().run_test("GeneratedTest")
"""

SUMMARY = re.compile(r"TESTS=(\d+) PASS=(\d+) FAIL=(\d+) SKIP=(\d+)\s+([\d.]+)")
KEEP = ("ERROR", "Error", "error", "Traceback", "assert", "FAIL",
        "Exception", 'File "', "Mismatch", "mismatch", "MISMATCH", "TIMEOUT")
ERR_NAME = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception))\b\s*:")
LINE_PREFIX = re.compile(r"^.*?\]:\s*")


def write_harness(workdir, dut_files, spec):
    """Create Makefile + test_generated.py for this DUT."""
    workdir = Path(workdir)
    sources = " ".join(str(Path(f).resolve()) for f in dut_files)
    (workdir / "Makefile").write_text(
        MAKEFILE.format(sources=sources, top=spec["module"]))

    clk = spec["clock_signal"]
    clock_line = (f'    cocotb.start_soon(Clock(dut.{clk}, 10, unit="ns").start())\n'
                  if clk else "")
    (workdir / "test_generated.py").write_text(
        TEST_ENTRY.format(clock_line=clock_line))


def _s(x):
    if x is None:
        return ""
    return x.decode(errors="replace") if isinstance(x, bytes) else x


def _result(kind, reason, log, ok=0, bad=0, sim_ns=0.0, mismatches=None):
    return {"kind": kind, "passed": kind == "pass", "reason": reason, "log": log,
            "pass_count": ok, "fail_count": bad, "sim_ns": sim_ns,
            "mismatches": mismatches or []}


def classify_failure(log, sim_ns, min_sim_ns):
    """Decide whether a failed run is a testbench error or scoreboard mismatches."""
    errs = ERR_NAME.findall(log)
    last_err = errs[-1] if errs else None
    lines = [l for l in log.splitlines() if l.strip()]
    strict = [l for l in lines if "MISMATCH" in l]
    loose = [l for l in lines if "mismatch" in l.lower()]
    mism = [LINE_PREFIX.sub("", l).strip() for l in (strict or loose)]
    if sim_ns >= min_sim_ns and last_err == "AssertionError" and mism:
        return "mismatch", mism
    return "tb_error", []


def run_sim(workdir, timeout=180, min_sim_ns=100.0):
    workdir = Path(workdir)
    shutil.rmtree(workdir / "sim_build", ignore_errors=True)
    (workdir / "results.xml").unlink(missing_ok=True)

    try:
        p = subprocess.run(["make"], cwd=workdir, capture_output=True,
                           text=True, timeout=timeout)
        log = _s(p.stdout) + "\n" + _s(p.stderr)
    except subprocess.TimeoutExpired as e:
        log = (_s(e.stdout) + "\n" + _s(e.stderr) +
               f"\nTIMEOUT: simulation exceeded {timeout}s (infinite loop, or the "
               "test never dropped its objection)")
        return _result("tb_error", f"timeout after {timeout}s", log)

    m = SUMMARY.search(log)
    if not m:
        return _result("tb_error", "compile or runtime error (no cocotb summary)", log)

    ok, bad, sim_ns = int(m.group(2)), int(m.group(3)), float(m.group(5))
    if bad > 0 or ok == 0:
        kind, mism = classify_failure(log, sim_ns, min_sim_ns)
        return _result(kind, f"{bad} test(s) failed", log, ok, bad, sim_ns, mism)
    if sim_ns < min_sim_ns:
        return _result("tb_error",
                       f"simulation ended at {sim_ns} ns, so the UVM test probably "
                       "never ran (false pass)", log, ok, bad, sim_ns)
    return _result("pass", "ok", log, ok, bad, sim_ns)


def trim_log(log, head=40, tail=30):
    """Keep the error-looking lines plus the end of the log, so the fix
    prompt stays small even when hundreds of mismatches are printed."""
    lines = [l.rstrip() for l in log.splitlines() if l.strip()]
    hits = [l for l in lines if any(k in l for k in KEEP)][:head]
    seen = set(hits)
    extra = [l for l in lines[-tail:] if l not in seen]
    return "\n".join(hits + ["... ..."] + extra)