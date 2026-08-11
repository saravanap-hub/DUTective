# DUTective

An automated UVM testbench generator. Point it at a Verilog/SystemVerilog module, and it parses the ports, generates a UVM-style testbench, runs it in simulation, checks the DUT against a gold model, and reports results — self-correcting its own testbench when the generated code fails to compile or run correctly.

Code is private for now, README is public to document progress.

---

## Pipeline

```
DUT (.v/.sv) → slang (port/width extraction → JSON)
             → prompt builder (JSON → testbench spec)
             → LLM codegen (Claude/Gemini, targeted per-component)
             → Icarus/Verilator (compile + simulate)
             → scoreboard vs. gold model → pass/fail + coverage
             → on failure: parse error → patch → retry
```

## Status

**Done:**
- Full toolchain set up: Icarus Verilog, Verilator (built from source), slang, pyuvm/cocotb, Docker sandboxing for sim runs
- Hand-built pyuvm testbench for a 32×32 register file DUT — sequence, driver, monitor, scoreboard, coverage, full UVM phasing (build/connect/run). Validated: 204/204 transactions matched an independent Python gold model, coverage report flagging real gaps (e.g. 31/32 registers hit under pure randomization)
- `slang` wired up to auto-extract ports, widths, and directions from any DUT as structured JSON
- Prompt structure that turns that port JSON into a testbench skeleton spec for the LLM to fill in

**Building now:**
- Compile-error self-correction loop — compile fails → parse the error → send only the broken snippet + error back to the LLM for a targeted patch → retry, bounded iterations

**Next:**
- Run the automated path on the register file DUT and compare its output against the hand-written testbench above
- Extend to a second DUT with a valid/ready handshake to test generalization beyond a simple register file
- Waveform diffing against the gold model for fault localization — pointing at *where* in the datapath a mismatch first appears, not just that one occurred

*(test output / coverage screenshots to be added here)*

## pyuvm vs. SystemVerilog UVM

Production verification runs SV UVM (IEEE 1800.2) on Questa/VCS/Xcelium. Those aren't accessible for free. Icarus/Verilator can't run full class-based SV UVM, but they can run pyuvm — the same methodology (same phases, same driver/monitor/scoreboard/agent structure) implemented in Python on cocotb. Same concepts, different syntax. Plan is to port this to real SV UVM once there's access to a proper simulator (Questa student edition).

## Tech stack

- **Simulation:** Icarus Verilog, Verilator
- **Verification methodology:** pyuvm on cocotb (SV UVM port planned)
- **Parsing:** slang — SystemVerilog port/width extraction to JSON
- **Orchestration:** Python
- **LLMs:** Claude, Gemini — targeted codegen + self-correction
- **Environment:** WSL2/Ubuntu, Docker for sandboxed sim execution

---

*Building in public.*
