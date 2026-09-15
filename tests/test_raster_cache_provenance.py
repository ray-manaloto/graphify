from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from graphify.cache import check_semantic_cache, save_semantic_cache
from graphify.raster import (
    RasterPreflightError,
    preflight_raster_batch,
    raster_attachment_compatibility,
)


FIXTURES = Path(__file__).parent / "fixtures" / "raster"


def _raster_compatibility(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    source = tmp_path / "alpha.png"
    shutil.copyfile(FIXTURES / "alpha.png", source)
    preflight = preflight_raster_batch(
        [{"path": "alpha.png", "label": "alpha.png"}],
        root=tmp_path,
    )
    return source, raster_attachment_compatibility(preflight)


def test_raster_semantic_cache_roundtrip_requires_same_attachment_identity(
    tmp_path: Path,
) -> None:
    source, compatibility = _raster_compatibility(tmp_path)

    assert (
        save_semantic_cache(
            [{"id": "image", "source_file": "alpha.png"}],
            [],
            root=tmp_path,
            attachment_compatibility=compatibility,
        )
        == 1
    )
    evidence: list[dict] = []
    nodes, _edges, _hyperedges, uncached = check_semantic_cache(
        [str(source)],
        root=tmp_path,
        attachment_compatibility=compatibility,
        cache_evidence_out=evidence,
    )

    assert [node["id"] for node in nodes] == ["image"]
    assert uncached == []
    assert evidence[0]["attachment_compatibility_fingerprint"] == next(iter(compatibility.values()))

    changed = {str(source): "0" * 64}
    nodes, _edges, _hyperedges, uncached = check_semantic_cache(
        [str(source)],
        root=tmp_path,
        attachment_compatibility=changed,
    )
    assert nodes == []
    assert uncached == [str(source)]


def test_raster_cache_read_with_compatibility_never_uses_legacy_entry(
    tmp_path: Path,
) -> None:
    source, _compatibility = _raster_compatibility(tmp_path)
    save_semantic_cache(
        [{"id": "legacy", "source_file": "alpha.png"}],
        [],
        root=tmp_path,
    )

    nodes, _edges, _hyperedges, uncached = check_semantic_cache(
        [str(source)],
        root=tmp_path,
        attachment_compatibility={},
    )

    assert nodes == []
    assert uncached == [str(source)]


def test_raster_cache_write_rejects_missing_attachment_identity_before_write(
    tmp_path: Path,
) -> None:
    source, _compatibility = _raster_compatibility(tmp_path)

    with pytest.raises(ValueError, match="attachment compatibility for every image"):
        save_semantic_cache(
            [{"id": "image", "source_file": str(source)}],
            [],
            root=tmp_path,
            attachment_compatibility={},
        )

    assert not (tmp_path / "graphify-out").exists()


def test_recovered_raster_path_rejects_attachment_for_original_label(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sub" / "alpha.png"
    source.parent.mkdir()
    shutil.copyfile(FIXTURES / "alpha.png", source)

    with pytest.raises(ValueError, match="attachment compatibility for every image"):
        save_semantic_cache(
            [{"id": "image", "source_file": "lost/alpha.png"}],
            [],
            root=tmp_path,
            allowed_source_files=["sub/alpha.png"],
            attachment_compatibility={"lost/alpha.png": "1" * 64},
        )

    assert not (tmp_path / "graphify-out").exists()


@pytest.mark.parametrize("include_original_label", [False, True])
def test_recovered_raster_path_uses_recovered_attachment_identity(
    tmp_path: Path, include_original_label: bool,
) -> None:
    source = tmp_path / "sub" / "alpha.png"
    source.parent.mkdir()
    shutil.copyfile(FIXTURES / "alpha.png", source)
    preflight = preflight_raster_batch(
        [{"path": "sub/alpha.png", "label": "sub/alpha.png"}], root=tmp_path,
    )
    compatibility = raster_attachment_compatibility(preflight)
    fingerprint = next(iter(compatibility.values()))
    if include_original_label:
        compatibility["lost/alpha.png"] = "1" * 64

    assert save_semantic_cache(
        [{"id": "image", "source_file": "lost/alpha.png"}],
        [],
        root=tmp_path,
        allowed_source_files=["sub/alpha.png"],
        attachment_compatibility=compatibility,
    ) == 1
    evidence: list[dict] = []
    nodes, _edges, _hyperedges, uncached = check_semantic_cache(
        [str(source)], root=tmp_path,
        attachment_compatibility={str(source): fingerprint},
        cache_evidence_out=evidence,
    )
    assert [node["id"] for node in nodes] == ["image"]
    assert Path(nodes[0]["source_file"]).resolve() == source.resolve()
    assert uncached == []
    assert evidence[0]["attachment_compatibility_fingerprint"] == fingerprint

    nodes, _edges, _hyperedges, uncached = check_semantic_cache(
        [str(source)], root=tmp_path,
        attachment_compatibility={str(source): "0" * 64},
    )
    assert nodes == [] and uncached == [str(source)]
    nodes, _edges, _hyperedges, uncached = check_semantic_cache(
        [str(source)], root=tmp_path,
    )
    assert nodes == [] and uncached == [str(source)]


def test_attachment_compatibility_rejects_conflicting_path_aliases(tmp_path: Path) -> None:
    source, compatibility = _raster_compatibility(tmp_path)
    fingerprint = next(iter(compatibility.values()))

    with pytest.raises(ValueError, match="conflicting path identities"):
        check_semantic_cache(
            [str(source)],
            root=tmp_path,
            attachment_compatibility={
                "alpha.png": fingerprint,
                str(source): "0" * 64,
            },
        )


def test_attachment_compatibility_rejects_duplicate_canonical_source_labels(
    tmp_path: Path,
) -> None:
    source, _compatibility = _raster_compatibility(tmp_path)
    preflight = preflight_raster_batch(
        [
            {"path": str(source), "label": "alpha.png"},
            {"path": str(source), "label": "alias.png"},
        ],
        root=tmp_path,
    )

    with pytest.raises(RasterPreflightError, match="conflicting canonical source"):
        raster_attachment_compatibility(preflight)


def test_non_raster_semantic_cache_remains_legacy_compatible(tmp_path: Path) -> None:
    source = tmp_path / "doc.md"
    source.write_text("# document\n")
    save_semantic_cache(
        [{"id": "doc", "source_file": "doc.md"}],
        [],
        root=tmp_path,
    )

    nodes, _edges, _hyperedges, uncached = check_semantic_cache(
        [str(source)],
        root=tmp_path,
    )

    assert [node["id"] for node in nodes] == ["doc"]
    assert uncached == []
