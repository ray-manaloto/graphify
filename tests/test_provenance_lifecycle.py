"""Cross-file entity evidence must track source replacement and portability."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from graphify.build import build, build_merge, merge_raw_extraction
from graphify.cache import _absolutize_source_files_in, _relativize_source_files_in
from graphify.dedup import deduplicate_entities
from graphify.watch import _rebase_relative_source_files, _relativize_source_files


def _merged_rationale() -> dict:
    nodes = [
        {"id": "findings_rationale", "label": "Rationale (column)",
         "file_type": "document", "source_file": "findings.md",
         "source_location": "L21"},
        {"id": "task_plan_rationale", "label": "Rationale (column)",
         "file_type": "document", "source_file": "task_plan.md",
         "source_location": "L69"},
    ]
    merged, _ = deduplicate_entities(nodes, [], communities={})
    assert len(merged) == 1
    return merged[0]


def _graph(tmp_path: Path, node: dict) -> Path:
    path = tmp_path / "graphify-out" / "graph.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"nodes": [node], "edges": [], "hyperedges": []}))
    return path


def test_raw_prune_removes_secondary_and_primary_contributors(tmp_path: Path) -> None:
    original = _merged_rationale()
    for pruned in ("findings.md", "task_plan.md"):
        path = _graph(tmp_path, original)
        result = merge_raw_extraction(
            {"nodes": [], "edges": [], "hyperedges": []}, path,
            prune_sources=[pruned], root=tmp_path,
        )
        assert len(result["nodes"]) == 1
        survivor = result["nodes"][0]
        assert survivor["source_file"] != pruned
        assert all(
            item["source_file"] != pruned
            for item in survivor.get("source_provenance", [])
        )


def test_raw_reextract_replaces_old_line_without_stale_evidence(tmp_path: Path) -> None:
    path = _graph(tmp_path, _merged_rationale())
    fresh = {"id": "new_task_plan_rationale", "label": "Rationale (column)",
             "file_type": "document", "source_file": "task_plan.md",
             "source_location": "L90"}
    result = merge_raw_extraction(
        {"nodes": [fresh], "edges": [], "hyperedges": []}, path,
        root=tmp_path,
    )
    current = [
        entry for node in result["nodes"]
        for entry in [node, *node.get("source_provenance", [])]
        if entry.get("source_file") == "task_plan.md"
    ]
    assert current
    assert {entry.get("source_location") for entry in current} == {"L90"}


def test_repeated_dedup_does_not_resurrect_replaced_line(tmp_path: Path) -> None:
    path = _graph(tmp_path, _merged_rationale())
    fresh = {"id": "new_task_plan_rationale", "label": "Rationale (column)",
             "file_type": "document", "source_file": "task_plan.md",
             "source_location": "L90"}
    raw = merge_raw_extraction(
        {"nodes": [fresh], "edges": [], "hyperedges": []}, path,
        root=tmp_path,
    )
    merged, _ = deduplicate_entities(raw["nodes"], [], communities={})
    provenance = [entry for node in merged
                  for entry in node.get("source_provenance", [])]
    assert ("task_plan.md", "L90") in {
        (entry["source_file"], entry["source_location"])
        for entry in provenance
    }
    assert ("task_plan.md", "L69") not in {
        (entry["source_file"], entry["source_location"])
        for entry in provenance
    }


def test_clustered_prune_retains_other_contributor(tmp_path: Path) -> None:
    for pruned in ("findings.md", "task_plan.md"):
        path = _graph(tmp_path, _merged_rationale())
        graph = build_merge([], path, prune_sources=[pruned],
                            root=tmp_path, dedup=False)
        assert graph.number_of_nodes() == 1
        attrs = next(iter(graph.nodes(data=True)))[1]
        assert attrs["source_file"] != pruned
        assert all(entry["source_file"] != pruned
                   for entry in attrs.get("source_provenance", []))


def test_nested_cache_and_watch_paths_rebase_to_new_root(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    destination = tmp_path / "destination"
    origin.mkdir()
    destination.mkdir()
    node = deepcopy(_merged_rationale())
    node["source_file"] = str(origin / "findings.md")
    for entry in node["source_provenance"]:
        entry["source_file"] = str(origin / entry["source_file"])
    payload = {"nodes": [node]}
    _relativize_source_files_in(payload, origin)
    assert {entry["source_file"] for entry in node["source_provenance"]} == {
        "findings.md", "task_plan.md"
    }
    _absolutize_source_files_in(payload, destination)
    assert {entry["source_file"] for entry in node["source_provenance"]} == {
        str(destination / "findings.md"), str(destination / "task_plan.md")
    }
    _relativize_source_files(payload, destination)
    _rebase_relative_source_files(payload, destination, tmp_path)
    assert {entry["source_file"] for entry in node["source_provenance"]} == {
        "destination/findings.md", "destination/task_plan.md"
    }


def test_build_normalizes_nested_provenance_paths(tmp_path: Path) -> None:
    node = deepcopy(_merged_rationale())
    node["source_file"] = str(tmp_path / node["source_file"])
    for entry in node["source_provenance"]:
        entry["source_file"] = str(tmp_path / entry["source_file"])
    graph = build([{"nodes": [node], "edges": []}], root=tmp_path, dedup=False)
    attrs = next(iter(graph.nodes(data=True)))[1]
    assert attrs["source_file"] == "findings.md"
    assert {entry["source_file"] for entry in attrs["source_provenance"]} == {
        "findings.md", "task_plan.md"
    }
