import io
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from ziptrip import _bootstrap


def test_cache_published_between_initial_checks(tmp_path, monkeypatch):
    destination = tmp_path / "digest"
    original_exists = Path.exists
    published = False

    def publish_on_exists(path):
        nonlocal published
        if path == destination and not published:
            published = True
            destination.mkdir()
            (destination / ".complete").write_text("1\n")
        return original_exists(path)

    monkeypatch.setattr(_bootstrap, "_cache_root", lambda: tmp_path)
    monkeypatch.setattr(Path, "exists", publish_on_exists)
    assert _bootstrap._unpack(None, "digest") == destination


@pytest.mark.parametrize(
    "name", ["app/../../escape", "site-packages/a\\..\\escape", "app/C:escape"]
)
def test_rejects_archive_traversal(tmp_path, name):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(name, "bad")
    buffer.seek(0)
    with ZipFile(buffer) as archive, pytest.raises(RuntimeError, match="unsafe archive member"):
        _bootstrap._extract(archive, tmp_path / "unpack")
    assert not (tmp_path / "escape").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permission checks")
def test_rejects_shared_writable_cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    monkeypatch.setenv("ZIPTRIP_CACHE_DIR", str(root))
    with pytest.raises(RuntimeError, match="not writable by others"):
        _bootstrap._cache_root()


@pytest.mark.parametrize("libc", ["musl", "unknown"])
def test_checks_linux_libc_family(options, monkeypatch, libc):
    from ziptrip.targets import get_target

    manifest = {
        "python_version": options.python_version,
        "target": get_target("x86_64-unknown-linux-gnu").as_dict(),
    }
    monkeypatch.setattr(_bootstrap.sys, "platform", "linux")
    monkeypatch.setattr(_bootstrap, "_machine", lambda: "x86_64")
    monkeypatch.setattr(_bootstrap, "_linux_libc", lambda: (libc, ""))
    with pytest.raises(RuntimeError, match="requires glibc"):
        _bootstrap._check_runtime(manifest)
