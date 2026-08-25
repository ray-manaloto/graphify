import json, sys
from pathlib import Path

pairs = [
    ("graph.py", "_source_path_evidence", "source_path_evidence"),
    ("graphify_baseline.py", "_verify_candidate", "verify_candidate"),
    ("skillopt_reviewed.py", "_reviewed_tasks", "reviewed_tasks"),
    ("handoff_reconcile.py", "Dropped", "dropped"),
    ("handoff_reconcile.py", "_is_commitment", "is_commitment"),
    ("artifact_download.py", "plan", "_provider_plan"),
    ("artifact_download.py", "download", "_provider_download"),
]

def load_labels_by_file(graph_path):
    data = json.loads(Path(graph_path).read_text())
    by_file = {}
    for n in data["nodes"]:
        sf = Path(n.get("source_file") or "").name
        label = (n.get("label") or "").rstrip("()").lstrip(".")
        by_file.setdefault(sf, []).append(label)
    return by_file

old_path, new_path = sys.argv[1], sys.argv[2]
old = load_labels_by_file(old_path)
new = load_labels_by_file(new_path)

print(f"{'file':<26}{'pair':<45}{'OLD (both present?)':<22}{'NEW (both present?)'}")
for fname, a, b in pairs:
    old_has_a = a in old.get(fname, [])
    old_has_b = b in old.get(fname, [])
    new_has_a = a in new.get(fname, [])
    new_has_b = b in new.get(fname, [])
    print(f"{fname:<26}{a+'/'+b:<45}{f'{old_has_a},{old_has_b}':<22}{f'{new_has_a},{new_has_b}'}")
