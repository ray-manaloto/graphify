"""Lifecycle helpers for nodes assembled from more than one source file."""

from __future__ import annotations

from collections.abc import Callable, Iterator


_SOURCE_BOUND_FIELDS = frozenset({
    "source_url", "author", "captured_at", "contributor", "rationale",
    "definition_file",
})


def source_path_records(item: dict) -> Iterator[dict]:
    """Yield the primary record and well-formed nested provenance records."""
    yield item
    for entry in item.get("source_provenance") or []:
        if isinstance(entry, dict):
            yield entry


def retain_live_provenance(
    node: dict, source_is_stale: Callable[[str], bool]
) -> dict | None:
    """Discard obsolete contributors without losing a still-backed entity.

    The primary source is a presentation choice after a cross-file merge. If it
    disappears, a remaining contributor becomes primary; the stable entity ID
    and its graph edges remain intact.
    """
    entries = node.get("source_provenance")
    if not isinstance(entries, list):
        return node
    kept = [
        entry for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("source_file"), str)
        and not source_is_stale(entry["source_file"])
    ]
    if not kept:
        return None
    result = dict(node)
    primary = result.get("source_file")
    promote_primary = not isinstance(primary, str) or source_is_stale(primary)
    if len(kept) != len(entries) or promote_primary:
        # Dedup can import scalar attributes from any losing contributor. Once
        # evidence is removed, these fields no longer have provable ownership.
        for field in _SOURCE_BOUND_FIELDS:
            result.pop(field, None)
    if promote_primary:
        result["source_file"] = kept[0]["source_file"]
        result["source_location"] = kept[0].get("source_location")
    if len(kept) > 1:
        result["source_provenance"] = kept
    else:
        result.pop("source_provenance", None)
    return result
