prompt_builder.py  (REUSABLE)
Turns a ports JSON + optional spec into the prompts sent to the LLM:
  build_prompt          first generation
  build_tb_fix_prompt   testbench crashed or misbehaved (edit blocks back)
  build_triage_prompt   only mismatches: testbench model wrong, or DUT wrong?
  build_rewrite_prompt  fallback when edit blocks could not be applied
"""
import json
import sys

CLOCK_NAMES = {"clk", "clock", "clk_i", "clk_in"}
RESET_NAMES = {"reset", "rst", "rst_n", "reset_n", "rstn"}
FENCE = "`" * 3

PATCH_FORMAT = """Reply format (choose exactly one):
1) To fix the testbench, reply with one or more edit blocks and nothing else:
<<<<<<< SEARCH
exact existing lines, copied character for character
=======
replacement lines
>>>>>>> REPLACE
Each SEARCH text must appear exactly once in the current file. Keep edits as
small as possible. Never return the whole file.
2) If the testbench correctly follows the SPECIFICATION and the DUT output
contradicts it, reply with a single line:
DUT_BUG: <signals, input values and time where behavior first diverged from the specification>"""


def classify_ports(ports):
    """Split ports into clock, reset, other inputs, and outputs."""
    clock, reset = None, None
    inputs, outputs = [], []
    for p in ports:
        name = p["name"].lower()
        if p["width"] == 1 and name in CLOCK_NAMES:
            clock = p
        elif p["width"] == 1 and name in RESET_NAMES:
            reset = p
        elif p["direction"] == "In":
            inputs.append(p)
        elif p["direction"] == "Out":
            outputs.append(p)
    return clock, reset, inputs, outputs


def build_spec(ports_data, intent_description=None):
    clock, reset, inputs, outputs = classify_ports(ports_data["ports"])
    return {
        "module": ports_data["module"],
        "clock_signal": clock["name"] if clock else None,
        "reset_signal": reset["name"] if reset else None,
        "control_inputs": inputs,
        "outputs": outputs,
        "intent_description": intent_description,  # None = no spec given
    }


def _describe_dut(spec):
    lines = [f"Module name: {spec['module']}",
             f"Clock signal: {spec['clock_signal']}",
             f"Reset signal: {spec['reset_signal']}",
             "\nControl/data inputs:"]
    for p in spec["control_inputs"]:
        lines.append(f"  - {p['name']} (width {p['width']} bits)")
    lines.append("\nOutputs:")
    for p in spec["outputs"]:
        lines.append(f"  - {p['name']} (width {p['width']} bits)")
    return lines


def _spec_block(spec):
    if spec["intent_description"]:
        return ["\nSPECIFICATION (the only source of truth for expected behavior):",
                spec["intent_description"]]
    return ["\nNo specification was provided. Infer behavior conservatively from "
            "port names and widths only, and put the comment UNVERIFIED MODEL "
            "at the top of the scoreboard."]


def build_prompt(spec):
    lines = ["Generate a pyuvm (UVM-on-cocotb) testbench for the following DUT.\n"]
    lines += _describe_dut(spec)
    lines += _spec_block(spec)
    lines.append(
        "\nGenerate: a sequence_item, a sequence with directed edge cases plus "
        "randomized transactions, a driver, a monitor, a scoreboard with an "
        "independent Python reference model, a coverage collector, an agent, an "
        "env, and the test class GeneratedTest."
    )
    if spec["intent_description"]:
        lines.append("The scoreboard model must implement exactly the behaviors in "
                     "the SPECIFICATION and nothing else.")
    return "\n".join(lines)


def build_tb_fix_prompt(spec, code, log, reason):
    """Full-context fix: the testbench crashed, timed out or ended too early."""
    lines = ["The testbench below failed when run against the DUT.\n"]
    lines += _describe_dut(spec)
    lines += _spec_block(spec)
    lines.append(f"\nFailure: {reason}")
    lines.append("\nCURRENT TESTBENCH:")
    lines.append(FENCE + "python\n" + code + FENCE)
    lines.append("\nSIMULATION LOG (trimmed):\n" + log)
    lines.append("\n" + PATCH_FORMAT)
    return "\n".join(lines)


def build_triage_prompt(spec, scoreboard_src, mismatches, counts):
    """Small prompt: the testbench ran fully and the scoreboard found mismatches."""
    lines = ["The testbench ran to completion, but its scoreboard reported mismatches.\n"]
    lines += _describe_dut(spec)
    lines += _spec_block(spec)
    lines.append("\n" + counts)
    lines.append("\nSCOREBOARD CODE (contains the reference model):")
    lines.append(FENCE + "python\n" + scoreboard_src + FENCE)
    lines.append("\nFIRST MISMATCHES:\n" + "\n".join(mismatches))
    lines.append(
        "\nDecide: does the reference model contradict the SPECIFICATION "
        "(testbench bug: fix it), or does the DUT output contradict the "
        "SPECIFICATION while the model follows it (DUT bug)?"
    )
    lines.append("\n" + PATCH_FORMAT)
    return "\n".join(lines)


def build_rewrite_prompt(spec, code, log, why):
    """Fallback: edit blocks could not be applied, so ask for the whole file."""
    lines = [f"Your edit blocks could not be applied ({why}).\n"]
    lines += _describe_dut(spec)
    lines += _spec_block(spec)
    lines.append("\nCURRENT TESTBENCH:")
    lines.append(FENCE + "python\n" + code + FENCE)
    lines.append("\nSIMULATION LOG (trimmed):\n" + log)
    lines.append(
        "\nReturn the FULL corrected testbench file in one code block, keeping "
        "all the rules from your instructions. If instead the testbench is "
        "correct and the DUT contradicts the SPECIFICATION, reply with a single "
        "line: DUT_BUG: <explanation>"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 prompt_builder.py <ports_json> [intent text]")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        data = json.load(f)
    intent = sys.argv[2] if len(sys.argv) > 2 else None
    print(build_prompt(build_spec(data, intent)))