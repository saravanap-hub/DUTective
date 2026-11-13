patcher.py  (REUSABLE)
The LLM returns small SEARCH/REPLACE edit blocks instead of a whole file.
This applies them to the current code, and pulls out single classes.
"""
import ast
import re

BLOCK = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> REPLACE",
    re.DOTALL,
)


def parse_patches(text):
    """Return a list of (old, new) pairs found in the LLM reply."""
    return [(m.group(1), m.group(2)) for m in BLOCK.finditer(text)]


def _find_unique(code, old):
    """Locate old inside code exactly once. Tries an exact match, then a match
    that ignores trailing spaces. Returns (start, end) or an error string."""
    n = code.count(old)
    if n == 1:
        i = code.index(old)
        return (i, i + len(old))
    if n > 1:
        return f"search text appears {n} times (must be unique)"

    old_lines = [l.rstrip() for l in old.split("\n")]
    code_lines = code.split("\n")
    k = len(old_lines)
    hits = [i for i in range(len(code_lines) - k + 1)
            if [l.rstrip() for l in code_lines[i:i + k]] == old_lines]
    if not hits:
        return "search text not found"
    if len(hits) > 1:
        return f"search text appears {len(hits)} times (must be unique)"
    start = sum(len(l) + 1 for l in code_lines[:hits[0]])
    end = start + sum(len(l) for l in code_lines[hits[0]:hits[0] + k]) + (k - 1)
    return (start, end)


def apply_patches(code, patches):
    """Apply all patches. Returns (new_code, error). Nothing is applied if
    any patch fails, or if the patched file is not valid Python."""
    new = code
    for k, (old, repl) in enumerate(patches, 1):
        found = _find_unique(new, old)
        if isinstance(found, str):
            return code, f"edit {k}: {found}"
        s, e = found
        new = new[:s] + repl + new[e:]
    try:
        ast.parse(new)
    except SyntaxError as err:
        return code, f"patched file has a syntax error: {err.msg} (line {err.lineno})"
    return new, None


def class_source(code, name_part):
    """Source of every class whose name contains name_part, e.g. 'Scoreboard'."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    parts = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and name_part.lower() in node.name.lower():
            seg = ast.get_source_segment(code, node)
            if seg:
                parts.append(seg)
    return "\n\n".join(parts) if parts else None