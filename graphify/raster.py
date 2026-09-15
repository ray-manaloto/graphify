"""Deterministic validation for raster inputs before managed CLI dispatch."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

RASTER_PREFLIGHT_SCHEMA_VERSION = 1
RASTER_ATTACHMENT_SCHEMA_VERSION = 1
MAX_RASTER_BYTES = 5 * 1024 * 1024
MAX_DIRECT_RASTERS = 20
MAX_RASTER_PIXELS = 40_000_000

_FORMAT_EXTENSIONS = {
    "PNG": {".png"},
    "JPEG": {".jpg", ".jpeg"},
    "GIF": {".gif"},
    "WEBP": {".webp"},
}
_ALLOWED_EXTENSIONS = {suffix for suffixes in _FORMAT_EXTENSIONS.values() for suffix in suffixes}
_REQUEST_KEYS = {"path", "label"}
_SOURCE_RECORD_KEYS = {"original_source", "preflight"}
_ORIGINAL_SOURCE_KEYS = {
    "label",
    "path",
    "canonical_path",
    "sha256",
    "byte_count",
    "decoded_format",
    "width",
    "height",
    "mode",
    "frame_count",
    "exif_orientation",
}
_PREFLIGHT_RECORD_KEYS = {"identity", "decoder", "limits", "batch_sha256"}
_ATTACHMENT_RECORD_KEYS = {
    "schema_version",
    "kind",
    "storage_mode",
    "original_source",
    "preflight",
    "staged_bytes",
    "lifecycle",
    "cli_preprocessing",
}
_DURABLE_STAGE_KEYS = {
    "status",
    "transport_path",
    "sha256",
    "byte_count",
    "raw_ref",
    "receipt_ref",
    "finalized",
}
_EPHEMERAL_STAGE_KEYS = {
    "status",
    "transport_path",
    "sha256",
    "byte_count",
    "finalized",
}
_LIFECYCLE_KEYS = {"scope", "id", "status"}
_CLI_PREPROCESSING_KEYS = {
    "status",
    "reported_sha256",
    "reported_byte_count",
    "reported_format",
    "reported_width",
    "reported_height",
    "reported_resize_mode",
}
_SNAPSHOT_ACK_KEYS = {
    "schema_version",
    "status",
    "raw_ref",
    "receipt_ref",
    "transport_path",
    "source_sha256",
    "source_byte_count",
    "staged_sha256",
    "staged_byte_count",
    "finalized",
}
_POLICY = {
    "schema_version": RASTER_PREFLIGHT_SCHEMA_VERSION,
    "formats": {key: sorted(value) for key, value in sorted(_FORMAT_EXTENSIONS.items())},
    "max_bytes": MAX_RASTER_BYTES,
    "max_direct_images": MAX_DIRECT_RASTERS,
    "max_pixels": MAX_RASTER_PIXELS,
    "animation": "reject",
    "integrity": "verify-and-load-from-hashed-bytes",
    "source_open": "retained-root-dirfd-component-nofollow-identity-nonblocking-final",
    "warning_filters": "caller-owned-no-mutation",
    "webp_header_admission": "bounded-riff-vp8-vp8l-vp8x-before-native-decoder",
}
_POLICY_SHA256 = hashlib.sha256(
    json.dumps(_POLICY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


class RasterPreflightError(ValueError):
    """A deterministic raster policy failure with JSON-safe details."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = {key: value for key, value in details.items() if value is not None}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": dict(self.details)}


def _fail(code: str, message: str, **details: Any) -> None:
    raise RasterPreflightError(code, message, **details)


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _SourceBinding:
    visible: Path
    canonical: Path
    relative_parts: tuple[str, ...]
    root_identity: _FileIdentity
    directory_identities: tuple[_FileIdentity, ...]
    file_identity: _FileIdentity


def _identity(info: os.stat_result) -> _FileIdentity:
    return _FileIdentity(info.st_dev, info.st_ino, info.st_mode)


def _require_secure_open_support() -> None:
    missing = [
        name for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK") if not hasattr(os, name)
    ]
    if os.open not in os.supports_dir_fd:
        missing.append("open(dir_fd=...)")
    if missing:
        _fail(
            "descriptor_security_unavailable",
            "secure root-anchored raster opening is unavailable on this platform",
            missing=sorted(missing),
        )


def _open_root(root: str | os.PathLike[str]) -> tuple[Path, int, _FileIdentity]:
    _require_secure_open_support()
    try:
        canonical = Path(root).resolve(strict=True)
        expected = os.lstat(canonical)
    except (FileNotFoundError, OSError) as exc:
        _fail("invalid_root", "raster root is unavailable", error_type=type(exc).__name__)
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
        _fail("invalid_root", "raster root must be a canonical directory", root=str(canonical))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(canonical, flags)
        actual = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        _fail("invalid_root", "raster root could not be opened", error_type=type(exc).__name__)
    identity = _identity(expected)
    if _identity(actual) != identity:
        os.close(descriptor)
        _fail("invalid_root", "raster root changed before descriptor binding")
    return canonical, descriptor, identity


def _load_pillow() -> tuple[Any, str]:
    try:
        import PIL
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        _fail(
            "decoder_unavailable",
            "raster validation requires the graphify[images] extra",
        )
    return (Image, UnidentifiedImageError), str(PIL.__version__)


def _request_value(request: Mapping[str, Any], key: str, index: int) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        _fail(
            "invalid_request",
            f"raster request {key} must be a non-empty string",
            index=index,
            field=key,
        )
    return value


def _resolve_source(
    root: Path,
    root_identity: _FileIdentity,
    source_path: str,
    *,
    index: int,
    label: str,
) -> _SourceBinding:
    requested = Path(source_path)
    visible = requested if requested.is_absolute() else root / requested
    try:
        canonical = visible.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        _fail(
            "source_unavailable",
            "raster source does not resolve to an available file",
            index=index,
            label=label,
            source_path=source_path,
            error_type=type(exc).__name__,
        )
    if not canonical.is_relative_to(root):
        _fail(
            "outside_root",
            "raster source resolves outside the allowed root",
            index=index,
            label=label,
            source_path=source_path,
            canonical_path=str(canonical),
        )
    relative_parts = canonical.relative_to(root).parts
    if not relative_parts:
        _fail(
            "not_regular_file",
            "raster source must be a regular file",
            index=index,
            label=label,
            source_path=source_path,
            canonical_path=str(canonical),
        )
    directory_identities: list[_FileIdentity] = []
    current = root
    try:
        for part in relative_parts[:-1]:
            current = current / part
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                _fail(
                    "source_unavailable",
                    "raster source parent is not a canonical directory",
                    index=index,
                    label=label,
                    source_path=source_path,
                )
            directory_identities.append(_identity(info))
        file_info = os.lstat(canonical)
    except RasterPreflightError:
        raise
    except OSError as exc:
        _fail(
            "source_unavailable",
            "raster source metadata is unavailable",
            index=index,
            label=label,
            source_path=source_path,
            error_type=type(exc).__name__,
        )
    if stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode):
        _fail(
            "not_regular_file",
            "raster source must be a regular file",
            index=index,
            label=label,
            source_path=source_path,
            canonical_path=str(canonical),
        )
    return _SourceBinding(
        visible=visible,
        canonical=canonical,
        relative_parts=relative_parts,
        root_identity=root_identity,
        directory_identities=tuple(directory_identities),
        file_identity=_identity(file_info),
    )


def _open_bound_source(root_descriptor: int, binding: _SourceBinding) -> int:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    file_flags |= getattr(os, "O_CLOEXEC", 0)
    current = os.dup(root_descriptor)
    file_descriptor: int | None = None
    try:
        if _identity(os.fstat(current)) != binding.root_identity:
            _fail("source_changed", "raster root descriptor identity changed")
        for part, expected in zip(
            binding.relative_parts[:-1], binding.directory_identities, strict=True
        ):
            child = os.open(part, directory_flags, dir_fd=current)
            os.close(current)
            current = child
            actual = os.fstat(current)
            if _identity(actual) != expected or not stat.S_ISDIR(actual.st_mode):
                _fail("source_changed", "raster source parent identity changed")
        file_descriptor = os.open(binding.relative_parts[-1], file_flags, dir_fd=current)
        actual_file = os.fstat(file_descriptor)
        if _identity(actual_file) != binding.file_identity or not stat.S_ISREG(actual_file.st_mode):
            _fail("source_changed", "raster source identity changed before reading")
        result = file_descriptor
        file_descriptor = None
        return result
    finally:
        os.close(current)
        if file_descriptor is not None:
            os.close(file_descriptor)


def _read_source(
    root_descriptor: int,
    binding: _SourceBinding,
    *,
    index: int,
    label: str,
    source_path: str,
) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = _open_bound_source(root_descriptor, binding)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                _fail(
                    "not_regular_file",
                    "raster source must remain a regular file while reading",
                    index=index,
                    label=label,
                    source_path=source_path,
                )
            if before.st_size > MAX_RASTER_BYTES:
                _fail(
                    "image_too_large",
                    "raster source exceeds the 5 MiB limit",
                    index=index,
                    label=label,
                    source_path=source_path,
                    size_bytes=before.st_size,
                    max_bytes=MAX_RASTER_BYTES,
                )
            payload = stream.read(MAX_RASTER_BYTES + 1)
            after = os.fstat(stream.fileno())
    except RasterPreflightError:
        raise
    except PermissionError as exc:
        _fail(
            "source_unreadable",
            "raster source could not be read",
            index=index,
            label=label,
            source_path=source_path,
            error_type=type(exc).__name__,
        )
    except OSError as exc:
        _fail(
            "source_changed",
            "raster source binding changed before it could be read",
            index=index,
            label=label,
            source_path=source_path,
            error_type=type(exc).__name__,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > MAX_RASTER_BYTES:
        _fail(
            "image_too_large",
            "raster source exceeds the 5 MiB limit",
            index=index,
            label=label,
            source_path=source_path,
            size_bytes=len(payload),
            max_bytes=MAX_RASTER_BYTES,
        )
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        _fail(
            "source_changed",
            "raster source changed while it was being read",
            index=index,
            label=label,
            source_path=source_path,
        )
    if len(payload) != before.st_size:
        _fail(
            "source_changed",
            "raster source byte count does not match its file metadata",
            index=index,
            label=label,
            source_path=source_path,
            expected_bytes=before.st_size,
            actual_bytes=len(payload),
        )
    try:
        current_visible = binding.visible.resolve(strict=True)
    except OSError as exc:
        _fail(
            "source_changed",
            "raster source path changed while it was being read",
            index=index,
            label=label,
            source_path=source_path,
            error_type=type(exc).__name__,
        )
    if current_visible != binding.canonical:
        _fail(
            "source_changed",
            "raster source path changed while it was being read",
            index=index,
            label=label,
            source_path=source_path,
        )
    return payload


def _riff_has_animation(payload: bytes) -> bool:
    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
        return False
    offset = 12
    while offset + 8 <= len(payload):
        kind = payload[offset : offset + 4]
        length = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        if kind in {b"ANIM", b"ANMF"}:
            return True
        offset += 8 + length + (length & 1)
    return False


def _webp_vp8_dimensions(chunk: bytes) -> tuple[int, int]:
    if len(chunk) < 10 or chunk[3:6] != b"\x9d\x01\x2a" or chunk[0] & 1:
        _fail("decode_failed", "WebP VP8 frame header is missing or invalid")
    width = int.from_bytes(chunk[6:8], "little") & 0x3FFF
    height = int.from_bytes(chunk[8:10], "little") & 0x3FFF
    if not width or not height:
        _fail("decode_failed", "WebP VP8 frame dimensions are invalid")
    return width, height


def _webp_vp8l_dimensions(chunk: bytes) -> tuple[int, int]:
    if len(chunk) < 5 or chunk[0] != 0x2F:
        _fail("decode_failed", "WebP VP8L frame header is missing or invalid")
    fields = int.from_bytes(chunk[1:5], "little")
    if fields >> 29:
        _fail("decode_failed", "WebP VP8L version is unsupported")
    return (fields & 0x3FFF) + 1, ((fields >> 14) & 0x3FFF) + 1


def _webp_vp8x_dimensions(chunk: bytes) -> tuple[int, int]:
    if len(chunk) != 10:
        _fail("decode_failed", "WebP VP8X header length is invalid")
    if chunk[0] & 0x02:
        _fail("animation_unsupported", "animated WebP inputs are unsupported")
    width = int.from_bytes(chunk[4:7], "little") + 1
    height = int.from_bytes(chunk[7:10], "little") + 1
    return width, height


def _preflight_webp_header(
    payload: bytes,
    *,
    index: int,
    label: str,
    source_path: str,
) -> None:
    """Bound WebP canvas allocation before Pillow constructs its native decoder."""
    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
        _fail("decode_failed", "WebP RIFF header is missing or truncated")
    declared_size = int.from_bytes(payload[4:8], "little")
    if declared_size < 4 or declared_size & 1 or declared_size + 8 != len(payload):
        _fail(
            "decode_failed",
            "WebP RIFF size does not exactly match the captured input",
            declared_size=declared_size,
            actual_size=len(payload) - 8,
        )

    dimensions: list[tuple[bytes, tuple[int, int]]] = []
    seen: set[bytes] = set()
    offset = 12
    while offset < len(payload):
        if offset + 8 > len(payload):
            _fail("decode_failed", "WebP chunk header is truncated")
        kind = payload[offset : offset + 4]
        length = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        data_start = offset + 8
        data_end = data_start + length
        padded_end = data_end + (length & 1)
        if data_end > len(payload) or padded_end > len(payload):
            _fail("decode_failed", "WebP chunk exceeds the captured RIFF bounds")
        if length & 1 and payload[data_end] != 0:
            _fail("decode_failed", "WebP chunk padding byte is invalid")
        chunk = payload[data_start:data_end]
        if kind in {b"ANIM", b"ANMF"}:
            _fail("animation_unsupported", "animated WebP inputs are unsupported")
        if kind in {b"VP8X", b"VP8 ", b"VP8L"}:
            if kind in seen:
                _fail("decode_failed", "WebP contains duplicate dimension-bearing chunks")
            seen.add(kind)
            if kind == b"VP8X":
                size = _webp_vp8x_dimensions(chunk)
            elif kind == b"VP8 ":
                size = _webp_vp8_dimensions(chunk)
            else:
                size = _webp_vp8l_dimensions(chunk)
            _require_pixel_limit(
                *size,
                index=index,
                label=label,
                source_path=source_path,
            )
            dimensions.append((kind, size))
        offset = padded_end

    if not dimensions:
        _fail("decode_failed", "WebP has no dimension-bearing image chunk")
    coded = [size for kind, size in dimensions if kind in {b"VP8 ", b"VP8L"}]
    if len(coded) != 1 or any(size != dimensions[0][1] for _kind, size in dimensions[1:]):
        _fail("decode_failed", "WebP canvas and coded-frame dimensions conflict")


def _png_has_animation(payload: bytes) -> bool:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    while offset + 12 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        kind = payload[offset + 4 : offset + 8]
        if kind == b"acTL":
            return True
        offset += 12 + length
    return False


def _animation_container(decoded_format: str, payload: bytes, info: Mapping[str, Any]) -> bool:
    if decoded_format == "PNG":
        return _png_has_animation(payload)
    if decoded_format == "GIF":
        return "loop" in info
    if decoded_format == "WEBP":
        return _riff_has_animation(payload)
    return False


def _inspect_image(
    payload: bytes,
    *,
    extension: str,
    index: int,
    label: str,
    source_path: str,
) -> dict[str, Any]:
    allowed_formats = sorted(_FORMAT_EXTENSIONS)
    if extension not in _ALLOWED_EXTENSIONS:
        _fail(
            "unsupported_format",
            "raster filename extension is unsupported",
            index=index,
            label=label,
            source_path=source_path,
            extension=extension,
        )
    if extension == ".webp" or (
        len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    ):
        _preflight_webp_header(
            payload,
            index=index,
            label=label,
            source_path=source_path,
        )
    (Image, UnidentifiedImageError), _decoder_version = _load_pillow()
    try:
        with Image.open(BytesIO(payload), formats=allowed_formats) as image:
            decoded_format = str(image.format or "").upper()
            width, height = image.size
            _require_pixel_limit(width, height, index=index, label=label, source_path=source_path)
            if decoded_format not in _FORMAT_EXTENSIONS:
                _fail(
                    "unsupported_format",
                    "decoded raster format is unsupported",
                    index=index,
                    label=label,
                    source_path=source_path,
                    decoded_format=decoded_format,
                )
            image.verify()
        with Image.open(BytesIO(payload), formats=allowed_formats) as decoded:
            _require_pixel_limit(*decoded.size, index=index, label=label, source_path=source_path)
            frame_count = int(getattr(decoded, "n_frames", 1))
            is_animated = bool(getattr(decoded, "is_animated", False))
            info = dict(decoded.info)
            mode = str(decoded.mode)
            exif_orientation = decoded.getexif().get(274)
            if (
                frame_count != 1
                or is_animated
                or _animation_container(decoded_format, payload, info)
            ):
                _fail(
                    "animation_unsupported",
                    "animated or multi-frame raster inputs are unsupported",
                    index=index,
                    label=label,
                    source_path=source_path,
                    decoded_format=decoded_format,
                    frame_count=frame_count,
                )
            decoded.load()
            if str(decoded.format or "").upper() != decoded_format or decoded.size != (
                width,
                height,
            ):
                _fail(
                    "decode_mismatch",
                    "raster metadata changed between integrity check and pixel decode",
                    index=index,
                    label=label,
                    source_path=source_path,
                )
    except RasterPreflightError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        _fail(
            "decompression_risk",
            "decoder rejected raster dimensions as a decompression risk",
            index=index,
            label=label,
            source_path=source_path,
            error_type=type(exc).__name__,
        )
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        _fail(
            "decode_failed",
            "raster integrity or pixel decoding failed",
            index=index,
            label=label,
            source_path=source_path,
            error_type=type(exc).__name__,
        )

    allowed_extensions = _FORMAT_EXTENSIONS[decoded_format]
    if extension not in allowed_extensions:
        _fail(
            "extension_mismatch",
            "raster filename extension does not match decoded content",
            index=index,
            label=label,
            source_path=source_path,
            extension=extension,
            decoded_format=decoded_format,
        )
    return {
        "decoded_format": decoded_format,
        "width": width,
        "height": height,
        "mode": mode,
        "frame_count": frame_count,
        "exif_orientation": exif_orientation,
    }


def _require_pixel_limit(
    width: int,
    height: int,
    *,
    index: int,
    label: str,
    source_path: str,
) -> None:
    if width * height > MAX_RASTER_PIXELS:
        _fail(
            "decompression_risk",
            "decoded raster dimensions exceed the pixel limit",
            index=index,
            label=label,
            source_path=source_path,
            width=width,
            height=height,
            max_pixels=MAX_RASTER_PIXELS,
        )


def preflight_raster_batch(
    requests: Sequence[Mapping[str, Any]],
    *,
    root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Validate one direct raster batch and return compact JSON-safe metadata.

    Exact source bytes are hashed and decoded one image at a time. They are not
    retained in the result; immutable transport snapshots remain a caller concern.
    """

    if isinstance(requests, (str, bytes)) or not isinstance(requests, Sequence):
        _fail("invalid_request", "raster requests must be a sequence of dictionaries")
    if len(requests) > MAX_DIRECT_RASTERS:
        _fail(
            "too_many_images",
            "direct raster batch exceeds the 20-image limit",
            image_count=len(requests),
            max_images=MAX_DIRECT_RASTERS,
        )
    (_image_module, _unidentified), decoder_version = _load_pillow()
    root_path, root_descriptor, root_identity = _open_root(root)
    records: list[dict[str, Any]] = []
    try:
        for index, request in enumerate(requests):
            if not isinstance(request, Mapping):
                _fail("invalid_request", "each raster request must be a dictionary", index=index)
            unknown = set(request) - _REQUEST_KEYS
            if unknown:
                _fail(
                    "invalid_request",
                    "raster request contains unsupported fields",
                    index=index,
                    fields=sorted(str(key) for key in unknown),
                )
            source_path = _request_value(request, "path", index)
            label = _request_value(request, "label", index)
            binding = _resolve_source(
                root_path,
                root_identity,
                source_path,
                index=index,
                label=label,
            )
            payload = _read_source(
                root_descriptor,
                binding,
                index=index,
                label=label,
                source_path=source_path,
            )
            inspected = _inspect_image(
                payload,
                extension=Path(source_path).suffix.lower(),
                index=index,
                label=label,
                source_path=source_path,
            )
            record = {
                "source_label": label,
                "source_path": source_path,
                "canonical_path": str(binding.canonical),
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                **inspected,
            }
            records.append(record)
    finally:
        os.close(root_descriptor)

    identity = {
        "name": "graphify.raster.preflight",
        "version": RASTER_PREFLIGHT_SCHEMA_VERSION,
        "policy_sha256": _POLICY_SHA256,
    }
    result = {
        "schema_version": RASTER_PREFLIGHT_SCHEMA_VERSION,
        "preflight_identity": identity,
        "decoder": {"name": "Pillow", "version": decoder_version},
        "limits": {
            "max_bytes": MAX_RASTER_BYTES,
            "max_direct_images": MAX_DIRECT_RASTERS,
            "max_pixels": MAX_RASTER_PIXELS,
        },
        "images": records,
    }
    result["batch_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def is_supported_raster_path(path: str | os.PathLike[str]) -> bool:
    """Return whether *path* has an extension owned by raster preflight."""

    return Path(path).suffix.lower() in _ALLOWED_EXTENSIONS


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        _fail(
            "invalid_attachment_record",
            f"{label} must contain exactly the supported fields",
            missing=sorted(expected - actual),
            unknown=sorted(str(key) for key in actual - expected),
        )


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("invalid_attachment_record", f"{label} must be a nonempty string")
    return value


def _require_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("invalid_attachment_record", f"{label} must be a nonnegative integer")
    return value


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        _fail("invalid_attachment_record", f"{label} must be a lowercase SHA-256 digest")
    return digest


def _json_copy(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    try:
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        _fail(
            "invalid_attachment_record",
            f"{label} must be JSON safe",
            error_type=type(exc).__name__,
        )
    return deepcopy(dict(value))


def raster_attachment_source_records(
    preflight: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize one preflight result into records shared by both stagers."""

    if not isinstance(preflight, Mapping):
        _fail("invalid_attachment_record", "preflight must be a dictionary")
    expected = {
        "schema_version",
        "preflight_identity",
        "decoder",
        "limits",
        "images",
        "batch_sha256",
    }
    _require_exact_keys(preflight, expected, "preflight")
    if preflight.get("schema_version") != RASTER_PREFLIGHT_SCHEMA_VERSION:
        _fail("invalid_attachment_record", "preflight schema version is unsupported")
    copy = _json_copy(preflight, "preflight")
    batch_sha256 = _require_sha256(copy.pop("batch_sha256"), "preflight.batch_sha256")
    actual_batch = hashlib.sha256(
        json.dumps(copy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if actual_batch != batch_sha256:
        _fail("invalid_attachment_record", "preflight batch digest does not match its content")
    identity = preflight.get("preflight_identity")
    decoder = preflight.get("decoder")
    limits = preflight.get("limits")
    images = preflight.get("images")
    if not all(isinstance(value, Mapping) for value in (identity, decoder, limits)):
        _fail("invalid_attachment_record", "preflight identity, decoder, and limits are required")
    if not isinstance(images, list):
        _fail("invalid_attachment_record", "preflight images must be a list")
    records: list[dict[str, Any]] = []
    for image in images:
        if not isinstance(image, Mapping):
            _fail("invalid_attachment_record", "preflight image must be a dictionary")
        expected_image_keys = {
            "source_label",
            "source_path",
            "canonical_path",
            "source_sha256",
            "size_bytes",
            "decoded_format",
            "width",
            "height",
            "mode",
            "frame_count",
            "exif_orientation",
        }
        _require_exact_keys(image, expected_image_keys, "preflight image")
        image_copy = _json_copy(image, "preflight image")
        original = {
            "label": image_copy.get("source_label"),
            "path": image_copy.get("source_path"),
            "canonical_path": image_copy.get("canonical_path"),
            "sha256": image_copy.get("source_sha256"),
            "byte_count": image_copy.get("size_bytes"),
            "decoded_format": image_copy.get("decoded_format"),
            "width": image_copy.get("width"),
            "height": image_copy.get("height"),
            "mode": image_copy.get("mode"),
            "frame_count": image_copy.get("frame_count"),
            "exif_orientation": image_copy.get("exif_orientation"),
        }
        record = {
            "original_source": original,
            "preflight": {
                "identity": deepcopy(dict(identity)),
                "decoder": deepcopy(dict(decoder)),
                "limits": deepcopy(dict(limits)),
                "batch_sha256": batch_sha256,
            },
        }
        _validate_attachment_source_record(record)
        records.append(record)
    return records


def _validate_attachment_source_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        _fail("invalid_attachment_record", "attachment source record must be a dictionary")
    _require_exact_keys(record, _SOURCE_RECORD_KEYS, "attachment source record")
    original = record.get("original_source")
    preflight = record.get("preflight")
    if not isinstance(original, Mapping) or not isinstance(preflight, Mapping):
        _fail("invalid_attachment_record", "attachment source sections must be dictionaries")
    _require_exact_keys(original, _ORIGINAL_SOURCE_KEYS, "original_source")
    _require_exact_keys(preflight, _PREFLIGHT_RECORD_KEYS, "preflight")
    for key in ("label", "path", "canonical_path", "decoded_format", "mode"):
        _require_string(original.get(key), f"original_source.{key}")
    _require_sha256(original.get("sha256"), "original_source.sha256")
    _require_count(original.get("byte_count"), "original_source.byte_count")
    _require_count(original.get("width"), "original_source.width")
    _require_count(original.get("height"), "original_source.height")
    _require_count(original.get("frame_count"), "original_source.frame_count")
    orientation = original.get("exif_orientation")
    if orientation is not None:
        _require_count(orientation, "original_source.exif_orientation")
    identity = preflight.get("identity")
    decoder = preflight.get("decoder")
    limits = preflight.get("limits")
    if not all(isinstance(value, Mapping) for value in (identity, decoder, limits)):
        _fail("invalid_attachment_record", "preflight sections must be dictionaries")
    _require_exact_keys(identity, {"name", "version", "policy_sha256"}, "preflight.identity")
    _require_exact_keys(decoder, {"name", "version"}, "preflight.decoder")
    _require_exact_keys(
        limits,
        {"max_bytes", "max_direct_images", "max_pixels"},
        "preflight.limits",
    )
    _require_string(identity.get("name"), "preflight.identity.name")
    _require_count(identity.get("version"), "preflight.identity.version")
    _require_sha256(identity.get("policy_sha256"), "preflight.identity.policy_sha256")
    _require_string(decoder.get("name"), "preflight.decoder.name")
    _require_string(decoder.get("version"), "preflight.decoder.version")
    for key in ("max_bytes", "max_direct_images", "max_pixels"):
        _require_count(limits.get(key), f"preflight.limits.{key}")
    _require_sha256(preflight.get("batch_sha256"), "preflight.batch_sha256")
    return _json_copy(record, "attachment source record")


def _unobserved_cli_preprocessing() -> dict[str, Any]:
    return {
        "status": "unobserved",
        "reported_sha256": None,
        "reported_byte_count": None,
        "reported_format": None,
        "reported_width": None,
        "reported_height": None,
        "reported_resize_mode": None,
    }


def _attachment_record(
    source_record: Mapping[str, Any],
    *,
    storage_mode: str,
    staged_bytes: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
) -> dict[str, Any]:
    source = _validate_attachment_source_record(source_record)
    return validate_raster_attachment_record(
        {
            "schema_version": RASTER_ATTACHMENT_SCHEMA_VERSION,
            "kind": "raster",
            "storage_mode": storage_mode,
            **source,
            "staged_bytes": dict(staged_bytes),
            "lifecycle": dict(lifecycle),
            "cli_preprocessing": _unobserved_cli_preprocessing(),
        }
    )


def validate_raster_attachment_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached JSON-safe copy of one normalized attachment record."""

    if not isinstance(record, Mapping):
        _fail("invalid_attachment_record", "raster attachment must be a dictionary")
    _require_exact_keys(record, _ATTACHMENT_RECORD_KEYS, "raster attachment")
    if record.get("schema_version") != RASTER_ATTACHMENT_SCHEMA_VERSION:
        _fail("invalid_attachment_record", "raster attachment schema is unsupported")
    if record.get("kind") != "raster":
        _fail("invalid_attachment_record", "attachment kind must be raster")
    source = _validate_attachment_source_record(
        {
            "original_source": record.get("original_source"),
            "preflight": record.get("preflight"),
        }
    )
    staged = record.get("staged_bytes")
    lifecycle = record.get("lifecycle")
    preprocessing = record.get("cli_preprocessing")
    if not all(isinstance(value, Mapping) for value in (staged, lifecycle, preprocessing)):
        _fail("invalid_attachment_record", "attachment evidence sections must be dictionaries")
    storage_mode = record.get("storage_mode")
    if storage_mode == "durable":
        _require_exact_keys(staged, _DURABLE_STAGE_KEYS, "staged_bytes")
        _require_string(staged.get("raw_ref"), "staged_bytes.raw_ref")
        _require_string(staged.get("receipt_ref"), "staged_bytes.receipt_ref")
        expected_scope = "caller_retained"
    elif storage_mode == "ephemeral_local":
        _require_exact_keys(staged, _EPHEMERAL_STAGE_KEYS, "staged_bytes")
        expected_scope = "request"
    else:
        _fail("invalid_attachment_record", "attachment storage_mode is unsupported")
    if staged.get("status") != "complete" or staged.get("finalized") is not True:
        _fail("invalid_attachment_record", "staged bytes must be complete and finalized")
    transport_path = _require_string(staged.get("transport_path"), "staged_bytes.transport_path")
    if not Path(transport_path).is_absolute() or any(
        marker in transport_path for marker in (",", "\0", "\n", "\r")
    ):
        _fail("invalid_attachment_record", "transport path must be safe and absolute")
    staged_sha256 = _require_sha256(staged.get("sha256"), "staged_bytes.sha256")
    staged_count = _require_count(staged.get("byte_count"), "staged_bytes.byte_count")
    original = source["original_source"]
    if staged_sha256 != original["sha256"] or staged_count != original["byte_count"]:
        _fail("invalid_attachment_record", "staged bytes differ from the admitted source")
    _require_exact_keys(lifecycle, _LIFECYCLE_KEYS, "lifecycle")
    if lifecycle.get("scope") != expected_scope:
        _fail("invalid_attachment_record", "attachment lifecycle scope does not match storage")
    _require_string(lifecycle.get("id"), "lifecycle.id")
    if lifecycle.get("status") not in {"active", "cleaned"}:
        _fail("invalid_attachment_record", "attachment lifecycle status is unsupported")
    _require_exact_keys(preprocessing, _CLI_PREPROCESSING_KEYS, "cli_preprocessing")
    if preprocessing.get("status") != "unobserved" or any(
        preprocessing.get(key) is not None for key in _CLI_PREPROCESSING_KEYS - {"status"}
    ):
        _fail("invalid_attachment_record", "CLI preprocessing must begin unobserved")
    return _json_copy(record, "raster attachment")


def verify_raster_snapshot_ack(
    acknowledgment: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any],
    snapshot_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Bind one caller-retained snapshot acknowledgment to observed bytes."""

    source = _validate_attachment_source_record(source_record)
    if not isinstance(acknowledgment, Mapping):
        _fail("invalid_snapshot_ack", "snapshot acknowledgment must be a dictionary")
    try:
        _require_exact_keys(acknowledgment, _SNAPSHOT_ACK_KEYS, "snapshot acknowledgment")
    except RasterPreflightError as exc:
        exc.code = "invalid_snapshot_ack"
        raise
    ack = _json_copy(acknowledgment, "snapshot acknowledgment")
    if ack.get("schema_version") != RASTER_ATTACHMENT_SCHEMA_VERSION:
        _fail("invalid_snapshot_ack", "snapshot acknowledgment schema is unsupported")
    if ack.get("status") != "complete" or ack.get("finalized") is not True:
        _fail("invalid_snapshot_ack", "snapshot acknowledgment must be complete and finalized")
    raw_ref = _require_string(ack.get("raw_ref"), "snapshot acknowledgment raw_ref")
    receipt_ref = _require_string(ack.get("receipt_ref"), "snapshot acknowledgment receipt_ref")
    transport_path = _require_string(
        ack.get("transport_path"), "snapshot acknowledgment transport_path"
    )
    if not Path(transport_path).is_absolute() or any(
        marker in transport_path for marker in (",", "\0", "\n", "\r")
    ):
        _fail("invalid_snapshot_ack", "snapshot transport path must be safe and absolute")
    original = source["original_source"]
    source_count = _require_count(
        ack.get("source_byte_count"), "snapshot acknowledgment source_byte_count"
    )
    staged_count = _require_count(
        ack.get("staged_byte_count"), "snapshot acknowledgment staged_byte_count"
    )
    source_sha256 = _require_sha256(
        ack.get("source_sha256"), "snapshot acknowledgment source_sha256"
    )
    staged_sha256 = _require_sha256(
        ack.get("staged_sha256"), "snapshot acknowledgment staged_sha256"
    )
    if source_sha256 != original["sha256"] or source_count != original["byte_count"]:
        _fail("snapshot_source_mismatch", "snapshot acknowledgment names a different source")
    root_path, root_descriptor, root_identity = _open_root(snapshot_root)
    try:
        binding = _resolve_source(
            root_path,
            root_identity,
            transport_path,
            index=0,
            label=original["label"],
        )
        if str(binding.canonical) != transport_path:
            _fail("invalid_snapshot_ack", "snapshot transport path must be canonical")
        payload = _read_source(
            root_descriptor,
            binding,
            index=0,
            label=original["label"],
            source_path=transport_path,
        )
    finally:
        os.close(root_descriptor)
    observed_sha256 = hashlib.sha256(payload).hexdigest()
    if (
        staged_sha256 != observed_sha256
        or staged_count != len(payload)
        or staged_sha256 != source_sha256
        or staged_count != source_count
    ):
        _fail("snapshot_bytes_mismatch", "snapshot bytes do not match the admitted source")
    return _attachment_record(
        source,
        storage_mode="durable",
        staged_bytes={
            "status": "complete",
            "transport_path": transport_path,
            "sha256": staged_sha256,
            "byte_count": staged_count,
            "raw_ref": raw_ref,
            "receipt_ref": receipt_ref,
            "finalized": True,
        },
        lifecycle={"scope": "caller_retained", "id": raw_ref, "status": "active"},
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if isinstance(written, bool) or not isinstance(written, int) or written <= 0:
            _fail("snapshot_write_failed", "ephemeral snapshot write did not make progress")
        offset += written


@contextmanager
def stage_ephemeral_raster_attachments(
    source_records: Sequence[Mapping[str, Any]],
    *,
    root: str | os.PathLike[str],
) -> Iterator[list[dict[str, Any]]]:
    """Stage exact-byte snapshots for one unmanaged request lifetime."""

    if isinstance(source_records, (str, bytes)) or not isinstance(source_records, Sequence):
        _fail("invalid_attachment_record", "source records must be a sequence")
    sources = [_validate_attachment_source_record(item) for item in source_records]
    root_path, root_descriptor, root_identity = _open_root(root)
    temporary_root = Path(tempfile.mkdtemp(prefix="graphify-raster-attachments-")).resolve()
    temporary_info = os.lstat(temporary_root)
    if not stat.S_ISDIR(temporary_info.st_mode) or stat.S_ISLNK(temporary_info.st_mode):
        os.close(root_descriptor)
        _fail("snapshot_setup_failed", "ephemeral snapshot root is not a regular directory")
    if any(marker in str(temporary_root) for marker in (",", "\0", "\n", "\r")):
        os.close(root_descriptor)
        shutil.rmtree(temporary_root)
        _fail("snapshot_setup_failed", "ephemeral snapshot root path is unsafe")
    lifecycle_id = hashlib.sha256(
        f"{temporary_info.st_dev}:{temporary_info.st_ino}:{temporary_root}".encode()
    ).hexdigest()
    records: list[dict[str, Any]] = []
    try:
        for index, source in enumerate(sources):
            original = source["original_source"]
            binding = _resolve_source(
                root_path,
                root_identity,
                original["path"],
                index=index,
                label=original["label"],
            )
            if str(binding.canonical) != original["canonical_path"]:
                _fail("source_changed", "raster source canonical path changed after preflight")
            payload = _read_source(
                root_descriptor,
                binding,
                index=index,
                label=original["label"],
                source_path=original["path"],
            )
            if (
                len(payload) != original["byte_count"]
                or hashlib.sha256(payload).hexdigest() != original["sha256"]
            ):
                _fail("source_changed", "raster source bytes changed after preflight")
            suffix = next(iter(sorted(_FORMAT_EXTENSIONS[original["decoded_format"]])))
            staged_path = temporary_root / f"image-{index:03d}{suffix}"
            descriptor = os.open(
                staged_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            staged_root, staged_root_descriptor, staged_root_identity = _open_root(temporary_root)
            try:
                staged_binding = _resolve_source(
                    staged_root,
                    staged_root_identity,
                    str(staged_path),
                    index=index,
                    label=original["label"],
                )
                staged_payload = _read_source(
                    staged_root_descriptor,
                    staged_binding,
                    index=index,
                    label=original["label"],
                    source_path=str(staged_path),
                )
            finally:
                os.close(staged_root_descriptor)
            if staged_payload != payload:
                _fail("snapshot_bytes_mismatch", "ephemeral snapshot verification failed")
            records.append(
                _attachment_record(
                    source,
                    storage_mode="ephemeral_local",
                    staged_bytes={
                        "status": "complete",
                        "transport_path": str(staged_path),
                        "sha256": original["sha256"],
                        "byte_count": original["byte_count"],
                        "finalized": True,
                    },
                    lifecycle={"scope": "request", "id": lifecycle_id, "status": "active"},
                )
            )
        yield records
    finally:
        os.close(root_descriptor)
        cleanup_error: RasterPreflightError | None = None
        try:
            current = os.lstat(temporary_root)
            if _identity(current) != _identity(temporary_info) or not stat.S_ISDIR(current.st_mode):
                _fail("snapshot_cleanup_failed", "ephemeral snapshot root identity changed")
            shutil.rmtree(temporary_root)
        except RasterPreflightError as exc:
            cleanup_error = exc
        except OSError as exc:
            cleanup_error = RasterPreflightError(
                "snapshot_cleanup_failed",
                "ephemeral snapshot cleanup failed",
                error_type=type(exc).__name__,
            )
        if cleanup_error is None:
            for record in records:
                record["lifecycle"]["status"] = "cleaned"
        else:
            raise cleanup_error


def raster_attachment_compatibility(
    preflight: Mapping[str, Any],
) -> dict[str, str]:
    """Return stable per-source cache fingerprints without transport references."""

    fingerprints: dict[str, str] = {}
    for source in raster_attachment_source_records(preflight):
        original = source["original_source"]
        payload = {
            "schema_version": RASTER_ATTACHMENT_SCHEMA_VERSION,
            "source": {
                key: original[key]
                for key in (
                    "label",
                    "canonical_path",
                    "sha256",
                    "byte_count",
                    "decoded_format",
                    "width",
                    "height",
                    "frame_count",
                )
            },
            "preflight_identity": source["preflight"]["identity"],
            "decoder": source["preflight"]["decoder"],
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        canonical_path = original["canonical_path"]
        prior = fingerprints.get(canonical_path)
        if prior is not None and prior != fingerprint:
            _fail(
                "conflicting_attachment_compatibility",
                "raster compatibility contains a conflicting canonical source",
                canonical_path=canonical_path,
            )
        fingerprints[canonical_path] = fingerprint
    return fingerprints
