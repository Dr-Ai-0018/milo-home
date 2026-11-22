import os
from contextlib import nullcontext
from pathlib import Path

import pytest
from private_test_runtime import cleanup_runtime, private_test_runtime, runtime_root

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root private runtime")


def identity(path):
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino


@pytest.mark.parametrize("failed", [False, True])
def test_cleanup_on_success_and_failure_keeps_external_symlink_target(tmp_path, failed):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(RuntimeError) if failed else nullcontext():
        with private_test_runtime("milo-cleanup-test-") as directory:
            assert directory.parent == runtime_root()
            assert directory.stat().st_mode & 0o777 == 0o700
            (directory / "outside").symlink_to(tmp_path, target_is_directory=True)
            (directory / "nested").mkdir()
            (directory / "nested/data").write_text("synthetic")
            if failed:
                raise RuntimeError("synthetic test failure")
    assert not directory.exists() and sentinel.read_text() == "keep"


def test_cleanup_refuses_outside_root_root_itself_and_wrong_identity(tmp_path):
    root = runtime_root()
    for path in (Path("/"), Path("/root"), root, tmp_path):
        with pytest.raises(ValueError):
            cleanup_runtime(path, identity(path))
        assert path.is_dir()
    with private_test_runtime("milo-cleanup-test-") as directory:
        with pytest.raises(ValueError, match="identity changed"):
            cleanup_runtime(directory, (-1, -1))
        assert directory.is_dir()


def test_cleanup_refuses_replaced_symlink(tmp_path):
    with private_test_runtime("milo-cleanup-test-") as directory:
        link = directory.with_name(directory.name + "-link")
        link.symlink_to(tmp_path, target_is_directory=True)
        try:
            with pytest.raises(ValueError):
                cleanup_runtime(link, identity(link))
            assert tmp_path.is_dir()
        finally:
            link.unlink()
