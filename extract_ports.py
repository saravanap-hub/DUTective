extract_ports.py  (REUSABLE)
Runs slang on a Verilog/SystemVerilog file, pulls out the port list
(name, direction, type, width) as clean JSON.

Usage: python3 extract_ports.py <verilog_file> [--top module_name]
"""
import argparse
import json
import re
import subprocess


def get_slang_ast(filepath):
    """Run slang, strip the leading text summary, return parsed JSON."""
    result = subprocess.run(
        ["slang", "--ast-json", "-", filepath],
        capture_output=True, text=True
    )
    stdout = result.stdout
    # slang prints a text summary before the JSON; find the first '{'
    json_start = stdout.find("{")
    if json_start == -1:
        raise RuntimeError(
            f"No JSON found in slang output. stderr was:\n{result.stderr}\n{stdout}"
        )
    return json.loads(stdout[json_start:])


def parse_width(type_str):
    """'logic[31:0]' -> 32, 'logic' -> 1, 'logic[3:0][7:0]' -> 32."""
    width = 1
    for hi, lo in re.findall(r"\[(\d+):(\d+)\]", type_str):
        width *= abs(int(hi) - int(lo)) + 1
    return width


def extract_ports(filepath, top=None):
    ast = get_slang_ast(filepath)
    instances = [m for m in ast["design"]["members"] if m.get("kind") == "Instance"]
    if not instances:
        raise RuntimeError("No top-level module found in " + filepath)

    names = [i["name"] for i in instances]
    if top:
        match = [i for i in instances if i["name"] == top]
        if not match:
            raise RuntimeError(f"Module '{top}' not found. Top-level modules: {names}")
        inst = match[0]
    elif len(instances) == 1:
        inst = instances[0]
    else:
        raise RuntimeError(f"Several top-level modules found: {names}. Pick one with --top.")

    ports = []
    for m in inst["body"].get("members", []):
        if m.get("kind") != "Port":
            continue
        ports.append({
            "name": m["name"],
            "direction": m["direction"],   # "In", "Out" or "InOut"
            "type": m["type"],
            "width": parse_width(m["type"]),
        })

    return {"module": inst["name"], "ports": ports}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--top", help="module name, if the file has several top-level modules")
    args = ap.parse_args()
    print(json.dumps(extract_ports(args.file, args.top), indent=2))