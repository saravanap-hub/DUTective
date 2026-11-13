# DUTective

Automated testbench generation and checking for Verilog and SystemVerilog modules.

DUTective takes a Verilog module and a short description of its intended behavior. It generates a pyuvm (UVM-style) testbench with an LLM, simulates it with Icarus Verilog, and reports whether the design behaves as described. If the generated testbench fails to compile or run, it repairs the testbench automatically, for up to three rounds. If the testbench is sound and the design contradicts the description, it reports a suspected DUT bug and leaves the testbench unchanged.

Status: early development. Validated on one design, a 32x32 register file. See [Testing](#testing) and [Known limitations](#known-limitations).

## How it works

```
DUT.v
  |  slang                    ports: name, direction, width
  v
prompt builder                ports + behavioral description
  |  Gemini
  v
pyuvm testbench
  |  Icarus Verilog + cocotb
  v
result
  |-- pass
  |-- testbench error      -> edit request -> patch -> re-run   (max 3 rounds)
  |-- scoreboard mismatch  -> "model wrong or DUT wrong?"
                              -> patch the model, or report a suspected DUT bug
```

1. **Port extraction.** slang elaborates the file and the module interface is read from its AST. Clock and reset are identified by name and width.
2. **Prompt construction.** The ports and the behavioral description are formatted into a prompt. The system prompt fixes the testbench structure (sequence item, sequence, driver, monitor, scoreboard with reference model, coverage, agent, env, test) and states the pyuvm API conventions that LLMs often get wrong.
3. **Generation.** Gemini returns a single-file testbench.
4. **Harness and simulation.** The runner writes a Makefile and a cocotb entry file for the DUT, runs the simulation, and classifies the outcome.
5. **Repair.** See below.
6. **Report.** The verdict, every attempt with its log, and token usage per call are written to `run_report.json`.

### Failure handling

| Outcome | How it is detected | Action |
|---|---|---|
| Compile error, Python exception, timeout | No cocotb summary, or an exception other than the scoreboard assertion | Send code and trimmed log, request an edit |
| Run ends under 100 ns | Simulation time check | Treated as a failure. The UVM test did not run |
| Scoreboard mismatches on a full-length run | `MISMATCH:` log lines and the scoreboard assertion | Send only the scoreboard class and the first five mismatches. The reply is a patch to the model or a `DUT_BUG` verdict |
| Pass | cocotb summary and sufficient simulation time | Verdict `PASS` |

Fixes are returned as SEARCH/REPLACE blocks. `patcher.py` applies a block only if its search text matches exactly once and the patched file still parses as Python. If an edit cannot be applied, one full rewrite is requested.

## Repository layout

| File | Purpose |
|---|---|
| `dutective_core/extract_ports.py` | Runs slang on a file and returns the module name, parameters and ports as JSON |
| `dutective_core/prompt_builder.py` | Builds the generation, fix, triage and rewrite prompts |
| `dutective_core/llm_client.py` | Gemini client with retry, model fallback and token accounting |
| `dutective_core/patcher.py` | Applies SEARCH/REPLACE edits and extracts single classes from the testbench |
| `dutective_core/sim_runner.py` | Writes the cocotb harness, runs the simulation, classifies failures |
| `dutective_core/run.py` | Command-line entry point that runs the full flow |
| `requirements.txt` | Python dependencies |

## Requirements

- Python 3.12 with cocotb 2.x, pyuvm, google-genai and python-dotenv
- Icarus Verilog 12
- slang, on `PATH`
- make
- A Gemini API key

Developed and tested on Ubuntu 24.04 under WSL2.

## Usage

```
git clone <repository> dutective
pip install -r dutective/requirements.txt
echo "GOOGLE_API_KEY=<key>" > .env
python3 dutective/dutective_core/run.py my_dut.v --intent "description of intended behavior"
```

| Argument | Description |
|---|---|
| `dut` | Verilog or SystemVerilog file |
| `--intent` | Intended behavior, as a text file path or an inline string |
| `--top` | Module to test, required when the file has several top-level modules |
| `--workdir` | Output folder, default `dutective_out` |
| `--max-fixes` | Maximum repair rounds, default 3 |

Output folder contents:

- `ports.json`: extracted interface
- `generated_tb.py`: the final testbench
- `attempts/`: each attempt and its simulation log
- `Makefile`, `test_generated.py`: the generated cocotb harness
- `run_report.json`: verdict, history of attempts, token usage

To list the model names available to your key: `python3 dutective/dutective_core/llm_client.py --list-models`. Set `GEMINI_MODEL` and `GEMINI_FALLBACK_MODELS` in `.env` to override the defaults.

## Behavioral description

The scoreboard's reference model is derived from the text passed in `--intent`, never from the RTL. A model read off the design under test inherits that design's bugs and cannot detect them. The description should state read and write timing, reset behavior and priority, and any state that is undefined at start-up.

If no description is given, the model is inferred from port names and widths only, and a passing run should be treated as unverified.

## Testing

Two checks are used: a hand-written reference testbench, and a mutation test, in which a known bug is planted in a copy of the DUT and the tool must report a failure.

DUT: 32-entry, 32-bit register file with two combinational read ports, one synchronous write port, and a synchronous active-high reset.

| Run | Result |
|---|---|
| Hand-written pyuvm testbench (reference, not in this repository) | 204 of 204 transactions matched the model. Coverage: 31 of 32 registers written |
| Generated testbench, correct DUT | Attempt 0 failed at 1 ns: `raise_objection(self)` was called with an argument, which pyuvm does not accept. Attempt 1, after one repair, passed 278 of 278 checks over 2785 ns |
| Generated testbench, DUT with write data incremented by one | Attempt 0 failed at 1 ns: `connect()` was called on an analysis export. Attempt 1 completed with 157 matches and 107 mismatches, each actual value exactly one higher than expected |

Example mismatch line from the last run:

```
MISMATCH: rst=0 wr=1 dst=3 wdata=0xa5000003 r1=3 r2=2 | EXP: rd1=0xa5000003 rd2=0xa5000002 | ACT: rd1=0xa5000004 rd2=0xa5000003
```

## Known limitations

- Validated on one design. Results on other blocks are not yet known.
- The description used for the register file was written from its RTL, so that check is not independent. For real designs it should come from a specification.
- Stimulus is random pin-level traffic. This fits blocks with a clock, a reset and data ports, such as register files, ALUs and FIFOs. Processors need a different approach: load a program and compare against an instruction-set model.
- A DUT must currently be a single file.
- Failures are reported at the module ports. Internal signals are not analyzed.
- Gemini is the only supported backend. API availability affects run time, and generation is not deterministic, so the number of repair rounds varies between runs.
- Testbenches use pyuvm on cocotb, not SystemVerilog UVM. The component structure, phases and TLM connections are the same, but randomization is hand-written, coverage is tracked manually, and there are no SVA assertions.

## Contributing

Useful areas:

- Designs of other types, with a description and the resulting `run_report.json`: FIFOs, FSMs, handshake interfaces, pipelined datapaths
- Multi-file DUT input and hierarchy handling
- A processor profile with program-based stimulus and a reference model
- Template-based generation of the fixed testbench parts (env, agent, test class)
- Internal-signal analysis for fault localization
- Support for other LLM backends
- A SystemVerilog UVM output target

To report a problem, open an issue with the DUT, the description used, the command, and `run_report.json`.
