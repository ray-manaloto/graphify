from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import warnings
import zlib
from pathlib import Path

import pytest

from graphify import raster


FIXTURES = Path(__file__).parent / "fixtures" / "raster"


def _request(path: Path, label: str | None = None) -> dict[str, str]:
    return {"path": str(path), "label": label or path.name}


def _error(requests: list[dict[str, str]], root: Path) -> raster.RasterPreflightError:
    with pytest.raises(raster.RasterPreflightError) as raised:
        raster.preflight_raster_batch(requests, root=root)
    json.dumps(raised.value.as_dict())
    return raised.value


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return len(payload).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")


def _oversized_dimension_png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return (
        signature + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", b"") + _png_chunk(b"IEND", b"")
    )


def _webp_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        kind + len(payload).to_bytes(4, "little") + payload + (b"\0" if len(payload) & 1 else b"")
    )


def _webp_container(*chunks: bytes) -> bytes:
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _vp8(width: int, height: int) -> bytes:
    header = b"\0\0\0\x9d\x01\x2a"
    header += width.to_bytes(2, "little") + height.to_bytes(2, "little")
    return _webp_chunk(b"VP8 ", header)


def _vp8l(width: int, height: int) -> bytes:
    fields = (width - 1) | ((height - 1) << 14)
    return _webp_chunk(b"VP8L", b"\x2f" + fields.to_bytes(4, "little"))


def _vp8x(width: int, height: int, *, flags: int = 0) -> bytes:
    payload = bytes([flags, 0, 0, 0])
    payload += (width - 1).to_bytes(3, "little")
    payload += (height - 1).to_bytes(3, "little")
    return _webp_chunk(b"VP8X", payload)


def _write_guarded_webp(
    tmp_path: Path,
    monkeypatch,
    payload: bytes,
) -> raster.RasterPreflightError:
    source = tmp_path / "guarded.webp"
    source.write_bytes(payload)
    from PIL import Image

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("Pillow must not receive a WebP rejected by the admission guard")

    monkeypatch.setattr(Image, "open", forbidden_open)
    return _error([_request(source)], tmp_path)


def test_fixture_manifest_matches_committed_bytes():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["generator_decoder"] == {"name": "Pillow", "version": "12.3.0"}
    for record in manifest["fixtures"]:
        payload = (FIXTURES.parents[2] / record["path"]).read_bytes()
        assert len(payload) == record["byte_count"]
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]


def test_static_formats_return_compact_deterministic_metadata():
    requests = [
        _request(FIXTURES / "alpha.png", "alpha source"),
        _request(FIXTURES / "oriented.jpg", "camera label"),
        _request(FIXTURES / "palette.gif"),
        _request(FIXTURES / "static.webp"),
        _request(FIXTURES / "grayscale.png"),
    ]

    first = raster.preflight_raster_batch(requests, root=FIXTURES)
    second = raster.preflight_raster_batch(requests, root=FIXTURES)

    assert first == second
    assert first["decoder"] == {"name": "Pillow", "version": "12.3.0"}
    assert [record["decoded_format"] for record in first["images"]] == [
        "PNG",
        "JPEG",
        "GIF",
        "WEBP",
        "PNG",
    ]
    assert first["images"][0]["mode"] == "RGBA"
    assert first["images"][1]["exif_orientation"] == 6
    assert first["images"][2]["mode"] == "P"
    assert first["images"][3]["mode"] == "RGB"
    assert first["images"][4]["mode"] == "L"
    assert first["images"][0]["source_label"] == "alpha source"
    assert first["images"][1]["source_label"] == "camera label"
    for request, record in zip(requests, first["images"], strict=True):
        payload = Path(request["path"]).read_bytes()
        assert record["source_sha256"] == hashlib.sha256(payload).hexdigest()
        assert record["size_bytes"] == len(payload)
        assert "payload" not in record
        assert "transport_path" not in record
    json.dumps(first)


def test_relative_source_and_inside_root_symlink_are_resolved(tmp_path: Path):
    source = tmp_path / "source.png"
    shutil.copyfile(FIXTURES / "alpha.png", source)
    alias = tmp_path / "alias.png"
    alias.symlink_to(source.name)

    result = raster.preflight_raster_batch([_request(Path("alias.png"))], root=tmp_path)

    assert result["images"][0]["source_path"] == "alias.png"
    assert result["images"][0]["canonical_path"] == str(source.resolve())


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("animated.gif", "animation_unsupported"),
        ("animated.webp", "animation_unsupported"),
        ("animated.png", "animation_unsupported"),
        ("single-frame-loop.gif", "animation_unsupported"),
        ("sample.bmp", "unsupported_format"),
        ("sample.mpo", "unsupported_format"),
    ],
)
def test_unsupported_formats_and_animation_containers_are_rejected(fixture: str, code: str):
    error = _error([_request(FIXTURES / fixture)], FIXTURES)
    assert error.code == code


def test_extension_mismatch_and_corruption_are_rejected(tmp_path: Path):
    mismatch = tmp_path / "wrong.jpg"
    shutil.copyfile(FIXTURES / "alpha.png", mismatch)
    assert _error([_request(mismatch)], tmp_path).code == "extension_mismatch"

    truncated = tmp_path / "truncated.png"
    truncated.write_bytes((FIXTURES / "alpha.png").read_bytes()[:24])
    assert _error([_request(truncated)], tmp_path).code == "decode_failed"


def test_missing_directory_and_outside_symlink_are_rejected(tmp_path: Path):
    assert _error([_request(tmp_path / "missing.png")], tmp_path).code == "source_unavailable"
    broken = tmp_path / "broken.png"
    broken.symlink_to("missing-target.png")
    assert _error([_request(broken)], tmp_path).code == "source_unavailable"
    directory = tmp_path / "directory.png"
    directory.mkdir()
    assert _error([_request(directory)], tmp_path).code == "not_regular_file"
    fifo = tmp_path / "fifo.png"
    os.mkfifo(fifo)
    assert _error([_request(fifo)], tmp_path).code == "not_regular_file"

    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    shutil.copyfile(FIXTURES / "alpha.png", outside)
    alias = tmp_path / "outside.png"
    alias.symlink_to(outside)
    try:
        assert _error([_request(alias)], tmp_path).code == "outside_root"
    finally:
        outside.unlink()


def test_final_file_swap_is_rejected_before_read(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.png"
    shutil.copyfile(FIXTURES / "alpha.png", source)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-swap.png"
    shutil.copyfile(FIXTURES / "alpha.png", outside)
    original_read = raster._read_source

    def swap_then_read(root_descriptor, binding, **kwargs):
        source.unlink()
        source.symlink_to(outside)
        return original_read(root_descriptor, binding, **kwargs)

    monkeypatch.setattr(raster, "_read_source", swap_then_read)
    try:
        assert _error([_request(source)], tmp_path).code == "source_changed"
    finally:
        outside.unlink()


def test_final_file_fifo_swap_is_nonblocking_and_rejected(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.png"
    shutil.copyfile(FIXTURES / "alpha.png", source)
    original_read = raster._read_source

    def swap_then_read(root_descriptor, binding, **kwargs):
        source.unlink()
        os.mkfifo(source)
        return original_read(root_descriptor, binding, **kwargs)

    monkeypatch.setattr(raster, "_read_source", swap_then_read)
    assert _error([_request(source)], tmp_path).code == "source_changed"


def test_ancestor_directory_swap_is_rejected(tmp_path: Path, monkeypatch):
    parent = tmp_path / "nested"
    parent.mkdir()
    source = parent / "source.png"
    shutil.copyfile(FIXTURES / "alpha.png", source)
    original_read = raster._read_source

    def swap_then_read(root_descriptor, binding, **kwargs):
        parent.rename(tmp_path / "original-nested")
        parent.mkdir()
        shutil.copyfile(FIXTURES / "alpha.png", parent / source.name)
        return original_read(root_descriptor, binding, **kwargs)

    monkeypatch.setattr(raster, "_read_source", swap_then_read)
    assert _error([_request(source)], tmp_path).code == "source_changed"


def test_size_count_and_decompression_limits_fail_closed(tmp_path: Path):
    too_large = tmp_path / "large.png"
    too_large.write_bytes(b"\0" * (raster.MAX_RASTER_BYTES + 1))
    assert _error([_request(too_large)], tmp_path).code == "image_too_large"

    requests = [_request(FIXTURES / "alpha.png", f"copy-{index}") for index in range(21)]
    assert _error(requests, FIXTURES).code == "too_many_images"
    accepted = raster.preflight_raster_batch(requests[:20], root=FIXTURES)
    assert len(accepted["images"]) == 20

    huge = tmp_path / "huge.png"
    huge.write_bytes(_oversized_dimension_png(100_000, 100_000))
    assert _error([_request(huge)], tmp_path).code == "decompression_risk"

    warning_sized = tmp_path / "warning-sized.png"
    warning_sized.write_bytes(_oversized_dimension_png(10_000, 10_000))
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        assert _error([_request(warning_sized)], tmp_path).code == "decompression_risk"


@pytest.mark.parametrize(
    "payload",
    [
        _webp_container(_vp8(10_000, 5_000)),
        _webp_container(_vp8l(10_000, 5_000)),
        _webp_container(_vp8x(10_000, 5_000), _vp8(10_000, 5_000)),
        _webp_container(_vp8x(100, 100), _vp8(10_000, 5_000)),
    ],
    ids=["vp8", "vp8l", "vp8x-canvas", "vp8-coded-frame"],
)
def test_webp_dimensions_are_bounded_before_pillow_open(
    tmp_path: Path, monkeypatch, payload: bytes
):
    assert _write_guarded_webp(tmp_path, monkeypatch, payload).code == "decompression_risk"


@pytest.mark.parametrize(
    "payload",
    [
        b"RIFF\x04\0\0\0WEBP",
        _webp_container(_webp_chunk(b"VP8X", b"short")),
        _webp_container(_vp8(8, 8), _vp8(8, 8)),
        _webp_container(_vp8x(8, 8), _vp8l(7, 8)),
        _webp_container(_vp8x(8, 8)),
        _webp_container(_vp8(8, 8)) + b"trailing",
        _webp_container(b"VP8 " + (8).to_bytes(4, "little") + b"abcd"),
        _webp_container(b"zzzz" + (1).to_bytes(4, "little") + b"x!" + _vp8(8, 8)),
    ],
    ids=[
        "missing-image-chunk",
        "truncated-vp8x",
        "duplicate-vp8",
        "conflicting-canvas",
        "missing-coded-frame",
        "trailing-bytes",
        "chunk-past-riff",
        "nonzero-padding",
    ],
)
def test_ambiguous_or_truncated_webp_is_rejected_before_pillow(
    tmp_path: Path, monkeypatch, payload: bytes
):
    assert _write_guarded_webp(tmp_path, monkeypatch, payload).code == "decode_failed"


@pytest.mark.parametrize(
    "payload",
    [
        _webp_container(_vp8x(8, 8, flags=0x02), _vp8(8, 8)),
        _webp_container(_webp_chunk(b"ANIM", b"\0" * 6), _vp8(8, 8)),
        _webp_container(_webp_chunk(b"ANMF", b"\0" * 16), _vp8(8, 8)),
    ],
    ids=["vp8x-animation-flag", "anim-chunk", "anmf-chunk"],
)
def test_webp_animation_markers_are_rejected_before_pillow(
    tmp_path: Path, monkeypatch, payload: bytes
):
    assert _write_guarded_webp(tmp_path, monkeypatch, payload).code == "animation_unsupported"


def test_webp_unknown_chunks_remain_admissible(tmp_path: Path):
    original = (FIXTURES / "static.webp").read_bytes()
    body = original[8:] + _webp_chunk(b"zzzz", b"opaque")
    source = tmp_path / "unknown.webp"
    source.write_bytes(b"RIFF" + len(body).to_bytes(4, "little") + body)

    result = raster.preflight_raster_batch([_request(source)], root=tmp_path)
    assert result["images"][0]["decoded_format"] == "WEBP"


def test_read_failure_is_reported_without_decoder_fallback(tmp_path: Path, monkeypatch):
    source = tmp_path / "unreadable.png"
    shutil.copyfile(FIXTURES / "alpha.png", source)
    original_open = os.open

    def denied(path, flags, mode=0o777, *, dir_fd=None):
        if path == source.name and dir_fd is not None:
            raise PermissionError("synthetic denial")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", denied)
    monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {denied})
    assert _error([_request(source)], tmp_path).code == "source_unreadable"


def test_validation_does_not_change_process_global_decoder_policy():
    from PIL import Image

    max_pixels = Image.MAX_IMAGE_PIXELS
    filters = list(warnings.filters)

    raster.preflight_raster_batch([_request(FIXTURES / "alpha.png")], root=FIXTURES)

    assert Image.MAX_IMAGE_PIXELS == max_pixels
    assert warnings.filters == filters


def test_decoder_does_not_override_concurrent_warning_policy(monkeypatch):
    from PIL import Image

    entered = threading.Event()
    release = threading.Event()
    original_open = Image.open
    worker_errors: list[BaseException] = []

    def blocking_open(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("warning overlap barrier timed out")
        return original_open(*args, **kwargs)

    def decode() -> None:
        try:
            raster.preflight_raster_batch([_request(FIXTURES / "alpha.png")], root=FIXTURES)
        except BaseException as exc:
            worker_errors.append(exc)

    monkeypatch.setattr(Image, "open", blocking_open)
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always", Image.DecompressionBombWarning)
        worker = threading.Thread(target=decode)
        worker.start()
        assert entered.wait(timeout=5)
        try:
            warnings.warn("concurrent control", Image.DecompressionBombWarning)
        finally:
            release.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        assert not worker_errors
        assert [str(item.message) for item in observed] == ["concurrent control"]


def test_platform_without_secure_descriptor_open_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    error = _error([_request(FIXTURES / "alpha.png")], tmp_path)
    assert error.code == "descriptor_security_unavailable"


def test_invalid_root_request_shape_and_missing_decoder_fail_closed(tmp_path: Path, monkeypatch):
    assert _error([_request(FIXTURES / "alpha.png")], tmp_path / "absent").code == "invalid_root"
    assert _error([{"path": str(FIXTURES / "alpha.png")}], FIXTURES).code == "invalid_request"
    assert (
        _error([{"path": str(FIXTURES / "alpha.png"), "label": "x", "extra": "y"}], FIXTURES).code
        == "invalid_request"
    )

    def unavailable():
        raise raster.RasterPreflightError("decoder_unavailable", "missing")

    monkeypatch.setattr(raster, "_load_pillow", unavailable)
    assert _error([], tmp_path).code == "decoder_unavailable"
