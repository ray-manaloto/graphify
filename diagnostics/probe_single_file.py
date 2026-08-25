import sys, json, ast
from pathlib import Path
sys.path.insert(0, "/tmp/graphify-fix")
from graphify.extract import extract_python

path = Path(sys.argv[1])
src = path.read_text(encoding="utf-8")
tree = ast.parse(src)
expected = sorted(
    n.name for n in ast.walk(tree)
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
)

result = extract_python(path)
if "error" in result:
    print("EXTRACTION ERROR:", result["error"])
    sys.exit(1)

got_labels = [n.get("label", "") for n in result.get("nodes", []) if n.get("_callable")]
got_names = set()
for lbl in got_labels:
    # label carries trailing "()" for callables per spec note
    got_names.add(lbl.rstrip("()"))

missing = [n for n in expected if n not in got_names]
print(f"file={path}")
print(f"expected_functions={len(expected)} got_callable_nodes={len(got_labels)} missing={len(missing)}")
print("missing names:", missing[:40])
