"""Sealed bytes remain fixed, including after import failure and source changes."""

import os
import struct
import zlib
from uuid import uuid4

import pytest
from test_public_works import content
from test_public_works import service as service

from milo_home.errors import ApiError
from milo_home.public_asset_store import png_dimensions, source_snapshot


def png(width=2, height=1):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\0" + b"\0\0\0" * width))
        + chunk(b"IEND", b"")
    )


def draft_and_source(service, tmp_path):
    draft = service.create(str(uuid4()), content())
    source = tmp_path / "selected.bin"
    source.write_bytes(b"sealed-original")
    return draft, source


def test_import_retry_cannot_replace_bytes_or_metadata(service, tmp_path):
    draft, source = draft_and_source(service, tmp_path)
    asset_id = str(uuid4())
    item = service.import_asset(draft["id"], asset_id, source)
    assert service.import_asset(draft["id"], asset_id, source) == item
    with pytest.raises(ApiError) as conflict:
        service.import_asset(draft["id"], asset_id, source, filename="different.bin")
    assert conflict.value.status == 409
    source.write_bytes(b"changed-source")
    with pytest.raises(ApiError):
        service.import_asset(draft["id"], asset_id, source)
    assert service.store.read(draft["id"], item) == b"sealed-original"
    source.unlink()
    assert service.store.read(draft["id"], item) == b"sealed-original"


def test_interruption_after_seal_leaves_unregistered_charged_bytes_and_exact_retry(
    service, tmp_path, monkeypatch
):
    draft, source = draft_and_source(service, tmp_path)
    original = service.store.seal

    def interrupt(*args):
        original(*args)
        raise ValueError("Synthetic interruption before registration")

    asset_id = str(uuid4())
    monkeypatch.setattr(service.store, "seal", interrupt)
    with pytest.raises(ValueError):
        service.import_asset(draft["id"], asset_id, source)
    assert service.show(draft["id"])["assets"] == []
    assert (service.store.root / "works" / draft["id"] / asset_id).exists()
    monkeypatch.setattr(service.store, "seal", original)
    service.store.max_bytes = len(source.read_bytes())
    with pytest.raises(ValueError):
        service.import_asset(draft["id"], str(uuid4()), source)
    item = service.import_asset(draft["id"], asset_id, source)
    assert service.store.read(draft["id"], item) == source.read_bytes()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "ancestor"])
def test_sources_cannot_escape_regular_file_checks(tmp_path, kind):
    source = tmp_path / "original.bin"
    source.write_bytes(b"data")
    selected = tmp_path / "selected.bin"
    if kind == "symlink":
        selected.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, selected)
    elif kind == "fifo":
        os.mkfifo(selected)
    elif kind == "ancestor":
        parent = tmp_path / "parent"
        parent.symlink_to(tmp_path, target_is_directory=True)
        selected = parent / source.name
    else:
        selected.mkdir()
    with pytest.raises((ValueError, OSError)):
        source_snapshot(selected)


def test_source_change_during_read_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "selected.bin"
    source.write_bytes(b"initial")
    original = os.fstat
    calls = 0

    def mutate(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_bytes(b"changed-size")
        return original(fd)

    monkeypatch.setattr(os, "fstat", mutate)
    with pytest.raises(ValueError, match="changed"):
        source_snapshot(source)


def test_png_cover_checks_bytes_and_dimensions_before_publication(service, tmp_path):
    draft, source = draft_and_source(service, tmp_path)
    source.write_bytes(png())
    asset = service.import_asset(draft["id"], str(uuid4()), source)
    cover = {"asset_id": asset["id"], "alt": "合成横图", "width": 3, "height": 1}
    revised = service.revise(draft["id"], content(cover=cover), 1)
    with pytest.raises(ValueError, match="dimensions"):
        service.publish(draft["id"], revised["version"])
    cover["width"] = 2
    revised = service.revise(draft["id"], content(cover=cover), revised["version"])
    service.publish(draft["id"], revised["version"])
    assert service.get(draft["id"])["cover"]["width"] == 2
    assert png_dimensions(source.read_bytes()) == (2, 1)
    for invalid in (b"not-png", png(0, 1), png(4001, 4000), b"\x89PNG\r\n\x1a\n" + b"x" * 25):
        with pytest.raises(ValueError):
            png_dimensions(invalid)
