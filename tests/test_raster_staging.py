from __future__ import annotations

import hashlib
import os
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

import graphify.raster as raster
from graphify.raster import (
    RasterPreflightError,
    preflight_raster_batch,
    raster_attachment_compatibility,
    raster_attachment_source_records,
    stage_ephemeral_raster_attachments,
    validate_raster_attachment_record,
    verify_raster_snapshot_ack,
)

FIXTURES = Path(__file__).parent / "fixtures" / "raster"


def _admitted(tmp_path: Path) -> tuple[Path, dict, dict]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "alpha.png"
    shutil.copyfile(FIXTURES / "alpha.png", source)
    preflight = preflight_raster_batch(
        [{"path": "alpha.png", "label": "alpha.png"}],
        root=source_root,
    )
    return source_root, preflight, raster_attachment_source_records(preflight)[0]


def _ack(source_record: dict, staged: Path) -> dict:
    original = source_record["original_source"]
    return {
        "schema_version": 1,
        "status": "complete",
        "raw_ref": "raw:raster:one",
        "receipt_ref": "receipt:raster:one",
        "transport_path": str(staged.resolve()),
        "source_sha256": original["sha256"],
        "source_byte_count": original["byte_count"],
        "staged_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        "staged_byte_count": staged.stat().st_size,
        "finalized": True,
    }


def test_source_records_and_compatibility_exclude_transport_state(tmp_path: Path) -> None:
    _root, preflight, source_record = _admitted(tmp_path)

    assert set(source_record) == {"original_source", "preflight"}
    assert source_record["preflight"]["batch_sha256"] == preflight["batch_sha256"]
    compatibility = raster_attachment_compatibility(preflight)

    assert compatibility == {
        source_record["original_source"]["canonical_path"]: next(iter(compatibility.values()))
    }
    assert len(next(iter(compatibility.values()))) == 64
    assert "transport" not in repr(compatibility)


def test_source_records_reject_tampered_preflight_digest(tmp_path: Path) -> None:
    _root, preflight, _source_record = _admitted(tmp_path)
    tampered = deepcopy(preflight)
    tampered["images"][0]["width"] += 1

    with pytest.raises(RasterPreflightError, match="batch digest"):
        raster_attachment_source_records(tampered)


def test_durable_ack_returns_detached_normalized_record(tmp_path: Path) -> None:
    _root, _preflight, source_record = _admitted(tmp_path)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    staged = snapshot_root / "image.png"
    shutil.copyfile(FIXTURES / "alpha.png", staged)
    acknowledgment = _ack(source_record, staged)

    record = verify_raster_snapshot_ack(
        acknowledgment,
        source_record=source_record,
        snapshot_root=snapshot_root,
    )

    assert record["storage_mode"] == "durable"
    assert record["staged_bytes"]["raw_ref"] == "raw:raster:one"
    assert record["lifecycle"] == {
        "scope": "caller_retained",
        "id": "raw:raster:one",
        "status": "active",
    }
    detached = validate_raster_attachment_record(record)
    detached["original_source"]["label"] = "changed"
    assert record["original_source"]["label"] == "alpha.png"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda ack: ack.update(source_byte_count=True), "nonnegative integer"),
        (lambda ack: ack.update(staged_sha256="0" * 64), "snapshot bytes"),
        (lambda ack: ack.pop("raw_ref"), "supported fields"),
        (lambda ack: ack.update(status="incomplete"), "complete and finalized"),
    ],
)
def test_durable_ack_rejects_invalid_evidence(tmp_path: Path, mutation, message: str) -> None:
    _root, _preflight, source_record = _admitted(tmp_path)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    staged = snapshot_root / "image.png"
    shutil.copyfile(FIXTURES / "alpha.png", staged)
    acknowledgment = _ack(source_record, staged)
    mutation(acknowledgment)

    with pytest.raises(RasterPreflightError, match=message):
        verify_raster_snapshot_ack(
            acknowledgment,
            source_record=source_record,
            snapshot_root=snapshot_root,
        )


def test_durable_ack_rejects_comma_and_symlink_paths(tmp_path: Path) -> None:
    _root, _preflight, source_record = _admitted(tmp_path)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    staged = snapshot_root / "image.png"
    shutil.copyfile(FIXTURES / "alpha.png", staged)
    comma = snapshot_root / "image,one.png"
    shutil.copyfile(staged, comma)
    comma_ack = _ack(source_record, comma)

    with pytest.raises(RasterPreflightError, match="safe and absolute"):
        verify_raster_snapshot_ack(
            comma_ack,
            source_record=source_record,
            snapshot_root=snapshot_root,
        )

    link = snapshot_root / "linked.png"
    link.symlink_to(staged)
    link_ack = _ack(source_record, staged)
    link_ack["transport_path"] = str(link)
    with pytest.raises(RasterPreflightError):
        verify_raster_snapshot_ack(
            link_ack,
            source_record=source_record,
            snapshot_root=snapshot_root,
        )


def test_ephemeral_stage_lives_for_context_and_cleans_only_its_root(tmp_path: Path) -> None:
    source_root, _preflight, source_record = _admitted(tmp_path)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("keep")

    with stage_ephemeral_raster_attachments([source_record], root=source_root) as records:
        record = records[0]
        staged = Path(record["staged_bytes"]["transport_path"])
        stage_root = staged.parent
        assert staged.read_bytes() == (source_root / "alpha.png").read_bytes()
        assert record["storage_mode"] == "ephemeral_local"
        assert set(record["staged_bytes"]) == {
            "status",
            "transport_path",
            "sha256",
            "byte_count",
            "finalized",
        }
        assert record["lifecycle"]["status"] == "active"

    assert not stage_root.exists()
    assert record["lifecycle"]["status"] == "cleaned"
    assert sentinel.read_text() == "keep"


def test_ephemeral_stage_can_cover_multiple_preflight_batches(tmp_path: Path) -> None:
    source_root, _preflight, source_record = _admitted(tmp_path)

    with stage_ephemeral_raster_attachments([source_record] * 21, root=source_root) as records:
        transport_paths = [record["staged_bytes"]["transport_path"] for record in records]

        assert len(records) == 21
        assert len(set(transport_paths)) == 21
        assert all(Path(path).is_file() for path in transport_paths)

    assert all(not Path(path).exists() for path in transport_paths)


def test_ephemeral_stage_rejects_changed_source_and_removes_partial_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, _preflight, source_record = _admitted(tmp_path)
    shutil.copyfile(FIXTURES / "grayscale.png", source_root / "alpha.png")
    stage_root = tmp_path / "stage"

    def _mkdtemp(*, prefix: str) -> str:
        assert prefix == "graphify-raster-attachments-"
        stage_root.mkdir()
        return str(stage_root)

    monkeypatch.setattr(raster.tempfile, "mkdtemp", _mkdtemp)

    with pytest.raises(RasterPreflightError, match="bytes changed"):
        with stage_ephemeral_raster_attachments([source_record], root=source_root):
            pytest.fail("changed source must fail before yielding")

    assert not stage_root.exists()


def test_ephemeral_cleanup_rejects_root_swap_without_following_link(tmp_path: Path) -> None:
    source_root, _preflight, source_record = _admitted(tmp_path)
    sentinel_root = tmp_path / "sentinel"
    sentinel_root.mkdir()
    sentinel = sentinel_root / "keep.txt"
    sentinel.write_text("keep")

    with pytest.raises(RasterPreflightError, match="root identity changed"):
        with stage_ephemeral_raster_attachments([source_record], root=source_root) as records:
            stage_root = Path(records[0]["staged_bytes"]["transport_path"]).parent
            moved = tmp_path / "moved-stage"
            stage_root.rename(moved)
            os.symlink(sentinel_root, stage_root, target_is_directory=True)

    assert sentinel.read_text() == "keep"
