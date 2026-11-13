run.py  (REUSABLE) - DUTective end-to-end runner.

  python3 dutective/dutective_core/run.py my_dut.v --intent my_dut_spec.txt
  python3 dutective/dutective_core/run.py my_dut.v --top my_module --intent "short description"

Flow: ports -> prompt -> Gemini -> simulate -> classify failure ->
      cheap fix (edit blocks / triage) -> simulate ... -> report + token usage
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import extract_ports
import llm_client
import patcher
import prompt_builder
import sim_runner

REQUIRED_TOOLS = ["slang", "iverilog", "make", "cocotb-config"]
MAX_MISMATCH_LINES = 5


def preflight():
    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    if missing:
        sys.exit("Missing tools: " + ", ".join(missing) +
                 "\nActivate your venv (cocotb) and check slang/iverilog installs.")


def load_intent(arg):
    """--intent can be a path to a text file or the description itself."""
    if not arg:
        return None
    try:
        p = Path(arg)
        if p.is_file():
            return p.read_text().strip()
    except OSError:
        pass
    return arg.strip()


def is_dut_bug(text):
    return (re.search(r"^\s*DUT_BUG\s*:", text, flags=re.M) is not None
            and "<<<<<<< SEARCH" not in text)


def interpret_reply(raw, code):
    """Turn a Gemini reply into ("dut_bug", note), ("code", new_code)
    or ("failed", reason)."""
    if is_dut_bug(raw):
        return "dut_bug", raw.strip()
    patches = patcher.parse_patches(raw)
    if patches:
        new, err = patcher.apply_patches(code, patches)
        if err:
            return "failed", err
        return "code", new
    if "class GeneratedTest" in raw:           # model sent a whole file instead
        return "code", llm_client.extract_code(raw)
    return "failed", "reply had neither edit blocks nor a full file"


def main():
    ap = argparse.ArgumentParser(description="DUTective: auto-generate and run a UVM testbench")
    ap.add_argument("dut", help="Verilog/SystemVerilog file")
    ap.add_argument("--intent", help="spec: path to a text file, or a short description")
    ap.add_argument("--workdir", default="dutective_out")
    ap.add_argument("--max-fixes", type=int, default=3)
    ap.add_argument("--top", help="top module name, if the DUT file has several top-level modules")
    args = ap.parse_args()

    preflight()
    llm_client.reset_usage()
    dut = Path(args.dut).resolve()
    work = Path(args.workdir).resolve()
    (work / "attempts").mkdir(parents=True, exist_ok=True)

    # 1. ports
    print("[1/4] extracting ports ...")
    ports_data = extract_ports.extract_ports(str(dut), args.top)
    (work / "ports.json").write_text(json.dumps(ports_data, indent=2))
    intent = load_intent(args.intent)
    spec = prompt_builder.build_spec(ports_data, intent)
    print(f"      module {spec['module']}: {len(spec['control_inputs'])} inputs, "
          f"{len(spec['outputs'])} outputs, clock={spec['clock_signal']}, "
          f"reset={spec['reset_signal']}")
    if not intent:
        print("      WARNING: no --intent given. The reference model will be a guess.")

    sim_runner.write_harness(work, [dut], spec)

    # 2. first generation
    print("[2/4] generating testbench ...")
    code = llm_client.extract_code(
        llm_client.generate(prompt_builder.build_prompt(spec), label="generate"))

    history, verdict, bug_note, evidence = [], None, None, []
    fixes_used, triaged = 0, False

    for attempt in range(args.max_fixes + 1):
        (work / "generated_tb.py").write_text(code)
        (work / "attempts" / f"attempt_{attempt}.py").write_text(code)

        # 3. simulate
        print(f"[3/4] simulating (attempt {attempt}) ...")
        res = sim_runner.run_sim(work)
        (work / "attempts" / f"attempt_{attempt}.log").write_text(res["log"])
        history.append({"attempt": attempt, "kind": res["kind"],
                        "passed": res["passed"], "reason": res["reason"],
                        "pass": res["pass_count"], "fail": res["fail_count"],
                        "sim_ns": res["sim_ns"]})

        if res["passed"]:
            verdict = "PASS"
            break
        print(f"      failed [{res['kind']}]: {res['reason']}")
        evidence = res["mismatches"][:MAX_MISMATCH_LINES]
        if attempt == args.max_fixes:
            verdict = "FAIL (out of fix attempts)"
            break

        # 4. fix
        fixes_used += 1
        log_small = sim_runner.trim_log(res["log"])
        scoreboard = patcher.class_source(code, "Scoreboard")
        if res["kind"] == "mismatch" and not triaged and scoreboard:
            triaged = True
            print(f"[4/4] mismatches only: short triage question ({fixes_used}/{args.max_fixes}) ...")
            counts = (f"{len(res['mismatches'])} mismatch line(s) in the log; "
                      f"simulation ran {res['sim_ns']} ns.")
            prompt = prompt_builder.build_triage_prompt(
                spec, scoreboard, evidence, counts)
            label = "triage"
        else:
            print(f"[4/4] asking Gemini for an edit ({fixes_used}/{args.max_fixes}) ...")
            prompt = prompt_builder.build_tb_fix_prompt(
                spec, code, log_small, res["reason"])
            label = "fix"

        raw = llm_client.generate(prompt, system=llm_client.FIX_SYSTEM_PROMPT,
                                  label=label)
        outcome, payload = interpret_reply(raw, code)

        if outcome == "failed":
            print(f"      edit not usable ({payload}); asking for a full rewrite ...")
            raw = llm_client.generate(
                prompt_builder.build_rewrite_prompt(spec, code, log_small, payload),
                label="rewrite")
            outcome, payload = interpret_reply(raw, code)

        if outcome == "dut_bug":
            verdict, bug_note = "SUSPECTED DUT BUG", payload
            break
        if outcome == "failed":
            verdict = "FAIL (could not apply a fix)"
            bug_note = payload
            break
        code = payload

    usage = llm_client.usage_summary()
    report = {"module": spec["module"], "verdict": verdict,
              "spec_provided": bool(intent), "fixes_used": fixes_used,
              "dut_bug_note": bug_note if verdict == "SUSPECTED DUT BUG" else None,
              "evidence": evidence, "history": history, "usage": usage}
    (work / "run_report.json").write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 60)
    print("RESULT:", verdict)
    print("Fix rounds used:", fixes_used)
    if verdict == "PASS" and not intent:
        print("NOTE: no spec was given, so treat this pass as UNVERIFIED.")
    if verdict == "SUSPECTED DUT BUG":
        print("Gemini says:", bug_note)
        for line in evidence[:3]:
            print("  evidence:", line)
        print("Review the spec and the DUT. The testbench was NOT edited to hide this.")
    elif bug_note:
        print("Detail:", bug_note)
    print(f"Tokens: in {usage['input_tokens']} | out {usage['output_tokens']} "
          f"| thinking {usage['thinking_tokens']} | {usage['calls']} call(s)")
    for c in usage["by_call"]:
        print(f"   {c['label']:<9} in {c['in']:>6}  out {c['out']:>6}  thinking {c['thinking']:>6}")
    print("Files:", work)


if __name__ == "__main__":
    main()