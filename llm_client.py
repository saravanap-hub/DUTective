llm_client.py  (REUSABLE)
Talks to Gemini with retry + fallback models, and counts tokens.
Debug: python3 llm_client.py --list-models
"""
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

CORE_DIR = Path(__file__).resolve().parent
load_dotenv(CORE_DIR.parent / ".env")   # repo-level .env, if any
load_dotenv(Path.cwd() / ".env")        # .env in the folder you run from

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.7-flash,gemini-3.6-flash,gemini-flash-latest",
    ).split(",")
    if m.strip()
]

FENCE = "`" * 3

SYSTEM_PROMPT = """You are an expert hardware verification engineer.
You write pyuvm testbenches that run on cocotb 2.x with Icarus Verilog.

Hard rules for the code you output:
1. Output ONE Python file only, inside a single code block. No explanations.
2. Import: cocotb triggers (RisingEdge, FallingEdge, Timer), pyuvm (from pyuvm import *), random.
3. Define a module-level variable DUT = None. The driver and monitor read
   it in build_phase (self.dut = DUT). Do NOT use ConfigDB.
4. Do NOT use @cocotb.test() or start a clock in this file. A separate
   entry file does that.
5. Use Timer(1, unit="ns") (the keyword is unit, not units).
6. Include: a uvm_sequence_item, a uvm_sequence (directed edge cases plus
   random transactions), a driver, a monitor with an analysis port,
   a scoreboard with an independent Python reference model, a coverage
   collector, an agent, an env, and a test class named exactly GeneratedTest.
7. The scoreboard must use uvm_tlm_analysis_fifo and check in check_phase,
   and report pass/fail counts in report_phase. It must fail the test
   (assert) if any mismatch was found.
8. Drive inputs on the falling clock edge. Sample in the monitor just after the rising edge.
9. If a SPECIFICATION section is present, derive the reference model ONLY from
   it, never from what such a design usually does. Start the scoreboard with a
   comment block titled SPEC MAPPING listing each modeled behavior and the spec
   point it comes from. If the spec does not define a behavior, do not model
   it; write a comment "UNSPECIFIED: ..." instead.
10. Obey every "undefined" or "do not check" statement in the specification.
11. Log each of the first 20 mismatches as ONE line that starts with MISMATCH:
    and includes the inputs, the expected value and the actual value. After
    that, only count them.
12. Run long enough to drive at least 200 transactions.
13. In pyuvm, raise_objection() and drop_objection() take NO arguments.
    Write self.raise_objection(), never self.raise_objection(self).
14. In report_phase, log one summary line starting with COVERAGE: that lists
    what was exercised (for example registers or opcodes hit, reset seen).
15. Keep the code compact: no docstrings, short comments only, no unused helpers.
16. In pyuvm, connect() exists only on PORTS. An analysis_export has no
    connect() method. Give the scoreboard (and the coverage collector) a
    uvm_tlm_analysis_fifo named fifo, and in the env's connect_phase write
    self.agent.monitor.ap.connect(self.scoreboard.fifo.analysis_export).
    Never create your own analysis_export inside a component.
"""

FIX_SYSTEM_PROMPT = """You are an expert hardware verification engineer repairing a
pyuvm testbench (cocotb 2.x, Icarus Verilog). Follow the reply format in the
user message exactly. Keep these true in the final code: module-level DUT
variable (no ConfigDB); Timer(1, unit="ns") with unit, not units;
raise_objection() and drop_objection() take no arguments; the scoreboard asserts
the fail count is zero in report_phase; every mismatch log line starts with
MISMATCH:. Never change the testbench just to hide a real DUT bug."""

# ---- token accounting -------------------------------------------------
USAGE = {"calls": []}


def reset_usage():
    USAGE["calls"] = []


def _record_usage(label, model, um):
    rec = {
        "label": label,
        "model": model,
        "in": (getattr(um, "prompt_token_count", 0) or 0) if um else 0,
        "out": (getattr(um, "candidates_token_count", 0) or 0) if um else 0,
        "thinking": (getattr(um, "thoughts_token_count", 0) or 0) if um else 0,
    }
    USAGE["calls"].append(rec)
    if um:
        print(f"      tokens [{label}]: in {rec['in']} | out {rec['out']} "
              f"| thinking {rec['thinking']}")
    else:
        print(f"      tokens [{label}]: not reported by the API")


def usage_summary():
    calls = USAGE["calls"]
    return {
        "calls": len(calls),
        "input_tokens": sum(c["in"] for c in calls),
        "output_tokens": sum(c["out"] for c in calls),
        "thinking_tokens": sum(c["thinking"] for c in calls),
        "by_call": calls,
    }


# ---- API ----------------------------------------------------------------
def get_client(timeout_ms=180000):
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY missing. Put it in a .env file in the "
                           "folder you run from.")
    return genai.Client(api_key=key,
                        http_options=types.HttpOptions(timeout=timeout_ms))


def list_models():
    client = get_client(timeout_ms=30000)
    print("Models available to your key (for generateContent):")
    for m in client.models.list():
        if "generateContent" in (getattr(m, "supported_actions", None) or []):
            print("  " + m.name.replace("models/", ""))


def generate(prompt, system=SYSTEM_PROMPT, model=MODEL,
             max_tokens=16000, retries=4, label="call"):
    """Send one prompt. Retries on overload, then tries fallback models."""
    client = get_client()
    config = types.GenerateContentConfig(system_instruction=system,
                                         max_output_tokens=max_tokens)
    for m in [model] + FALLBACK_MODELS:
        for attempt in range(retries):
            try:
                parts, last_usage = [], None
                for chunk in client.models.generate_content_stream(
                        model=m, contents=prompt, config=config):
                    if getattr(chunk, "usage_metadata", None):
                        last_usage = chunk.usage_metadata
                    if chunk.text:
                        parts.append(chunk.text)
                        print(".", end="", flush=True)
                print()
                _record_usage(label, m, last_usage)
                return "".join(parts)
            except errors.ServerError as e:        # 5xx: Google overloaded
                wait = 5 * 2 ** attempt
                print(f"\n[{m}] server busy ({e.code}), retrying in {wait}s ...")
                time.sleep(wait)
            except errors.ClientError as e:
                if e.code == 404:                  # name not available
                    print(f"\n[{m}] not found, trying next model ...")
                    break
                if e.code == 429:                  # rate limit
                    wait = 15 * (attempt + 1)
                    print(f"\n[{m}] rate limited, retrying in {wait}s ...")
                    time.sleep(wait)
                else:
                    raise
    raise RuntimeError("All Gemini models and retries failed. Try again later.")


def extract_code(text):
    """Pull code out of a markdown fence if the model used one."""
    pattern = FENCE + r"(?:python)?\s*\n(.*?)" + FENCE
    blocks = re.findall(pattern, text, flags=re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"


if __name__ == "__main__":
    if "--list-models" in sys.argv:
        list_models()
    else:
        print("Use run.py. For debugging: python3 llm_client.py --list-models")