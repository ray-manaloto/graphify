import sys, json
from pathlib import Path
sys.path.insert(0, "/tmp/graphify-fix")
from graphify.extract import extract_python

path = Path(sys.argv[1])
result = extract_python(path)
for n in result.get("nodes", []):
    if n.get("_callable"):
        print(json.dumps({"id": n.get("id"), "label": n.get("label"), "loc": n.get("source_location")}))
