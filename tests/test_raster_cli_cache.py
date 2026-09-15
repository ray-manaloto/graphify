"""Public CLI controls for raster admission before semantic-cache reads."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import graphify.__main__ as mainmod
from graphify import cache, llm, raster


def _run_extract(monkeypatch, corpus: Path, out: Path) -> None:
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _path: None)
    monkeypatch.setattr(
        mainmod.sys,
        "argv",
        [
            "graphify",
            "extract",
            str(corpus),
            "--backend",
            "claude",
            "--out",
            str(out),
        ],
    )
    mainmod.main()


def test_corrupt_raster_cannot_succeed_through_legacy_cache(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "broken.png"
    source.write_bytes(b"not a raster")
    out = tmp_path / "out"
    cache.save_semantic_cache(
        [
            {
                "id": "legacy-image",
                "label": "legacy image",
                "type": "concept",
                "source_file": "broken.png",
            }
        ],
        [],
        root=corpus,
        cache_root=out,
        allowed_source_files=[str(source)],
        prompt=llm._extraction_system(),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setattr(
        llm,
        "extract_corpus_parallel",
        lambda *_args, **_kwargs: pytest.fail("invalid raster must fail before extraction"),
    )

    with pytest.raises(raster.RasterPreflightError):
        _run_extract(monkeypatch, corpus, out)


def test_compatible_raster_cache_entry_remains_a_warm_hit(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "valid.png"
    shutil.copyfile(Path(__file__).parent / "fixtures/raster/alpha.png", source)
    out = tmp_path / "out"
    admission = llm._preflight_raster_cache_admission([source], root=corpus)
    compatibility = admission["attachment_compatibility"]
    assert compatibility
    cache.save_semantic_cache(
        [
            {
                "id": "cached-image",
                "label": "cached image",
                "type": "concept",
                "source_file": "valid.png",
            }
        ],
        [],
        root=corpus,
        cache_root=out,
        allowed_source_files=[str(source)],
        prompt=llm._extraction_system(),
        attachment_compatibility=compatibility,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setattr(
        llm,
        "extract_corpus_parallel",
        lambda *_args, **_kwargs: pytest.fail("compatible warm hit must skip extraction"),
    )

    _run_extract(monkeypatch, corpus, out)

    graph = out / "graphify-out/graph.json"
    assert graph.is_file()
    assert "cached-image" in graph.read_text(encoding="utf-8")
