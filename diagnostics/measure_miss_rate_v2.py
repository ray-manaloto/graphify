import sys, json, ast
from pathlib import Path

pkg = Path("/Users/rmanaloto/dev/github/ray-manaloto/knowledge-base/python/src/kb_setup")
graph_path = Path(sys.argv[1])
data = json.loads(graph_path.read_text())
nodes = data["nodes"]

present = set()  # (basename, normalized_label)
for n in nodes:
    if not n.get("_callable"):
        continue
    sf = n.get("source_file") or ""
    label = (n.get("label") or "").rstrip("()")
    label = label.lstrip(".")  # strip method-label leading dot
    present.add((Path(sf).name, label))

total_expected = 0
total_missing = 0
missing_files = []

for py in sorted(pkg.rglob("*.py")):
    src = py.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"SYNTAX ERROR in {py}: {e}")
        continue
    names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    total_expected += len(names)
    missing_here = []
    for nm in names:
        if (py.name, nm) not in present:
            missing_here.append(nm)
    if missing_here:
        total_missing += len(missing_here)
        missing_files.append((py.name, len(names), len(missing_here), missing_here[:15]))

print(f"total_expected={total_expected} total_missing={total_missing} rate={total_missing/total_expected:.3f}")
print(f"files_with_any_missing={len(missing_files)}")
for name, exp, miss, sample in missing_files:
    print(f"  {name}: {miss}/{exp} missing, e.g. {sample}")
