"""Standalone zipapp launcher. This file is copied verbatim into each archive."""

import hashlib
import importlib
import json
import os
import platform
import runpy
import shutil
import site
import stat
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def _machine():
    if sys.platform == "win32":
        return {"win32": "i686", "win-amd64": "x86_64", "win-arm64": "aarch64"}.get(
            sysconfig.get_platform(), platform.machine().lower()
        )
    name = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64", "i386": "i686"}.get(name, name)


def _linux_libc():
    name, version = platform.libc_ver()
    if name == "glibc":
        return name, version
    try:
        if "ld-musl-" in Path("/proc/self/maps").read_text():
            return "musl", ""
    except OSError:
        pass
    if "musl" in (sysconfig.get_config_var("HOST_GNU_TYPE") or ""):
        return "musl", ""
    return name or "unknown", version


def _check_runtime(manifest):
    expected = tuple(map(int, manifest["python_version"].split(".")))
    actual = sys.version_info[:3]
    if (
        sys.implementation.name != "cpython"
        or actual[:2] != expected[:2]
        or (len(expected) == 3 and actual < expected)
        or sysconfig.get_config_var("Py_GIL_DISABLED")
    ):
        raise RuntimeError(
            f"requires GIL-enabled CPython {manifest['python_version']} "
            f"(same minor version); running {platform.python_implementation()} "
            f"{platform.python_version()}"
        )
    target = manifest["target"]
    if sys.platform != target["system"] or _machine() != target["machine"]:
        raise RuntimeError(f"built for {target['triple']}; running {sys.platform}/{_machine()}")
    if target["libc"]:
        name, version = _linux_libc()
        if name != target["libc"]:
            raise RuntimeError(f"requires {target['baseline']}; detected libc: {name}")
        if name == "glibc" and tuple(map(int, version.split(".")[:2])) < (2, 28):
            raise RuntimeError(f"requires glibc 2.28+; detected {version}")
    if sys.platform == "darwin":
        if tuple(map(int, platform.mac_ver()[0].split(".")[:2])) < (13, 0):
            raise RuntimeError("requires macOS 13 or newer")


def _cache_root():
    override = os.environ.get("ZIPTRIP_CACHE_DIR")
    if override:
        root = Path(override).expanduser()
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        root /= "ziptrip"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ziptrip"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"cache must be a real directory, not a symlink: {root}")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise RuntimeError(f"cache must be owned by you and not writable by others: {root}")
    return root.resolve()


def _extract(archive, destination):
    for member in archive.infolist():
        path = PurePosixPath(member.filename)
        if not path.parts or path.parts[0] not in {"app", "site-packages"}:
            continue
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in member.filename
            or ":" in member.filename
            or stat.S_ISLNK(member.external_attr >> 16)
        ):
            raise RuntimeError(f"unsafe archive member: {member.filename}")
        output = destination.joinpath(*path.parts)
        if member.is_dir():
            output.mkdir(parents=True, exist_ok=True)
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, output.open("wb") as target:
            shutil.copyfileobj(source, target)
        output.chmod(0o755 if member.external_attr >> 16 & 0o111 else 0o644)


def _unpack(archive, digest):
    cache = _cache_root()
    destination = cache / digest
    if destination.is_symlink():
        raise RuntimeError(f"cache entry must not be a symlink: {destination}")
    if destination.exists():
        if (destination / ".complete").is_file():
            return destination
        raise RuntimeError(f"incomplete cache entry; remove it and retry: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=cache))
    try:
        _extract(archive, temporary)
        (temporary / ".complete").write_text("1\n")
        try:
            temporary.rename(destination)
        except OSError:
            # Another process may have published the same complete archive first.
            if destination.is_symlink() or not (destination / ".complete").is_file():
                raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def _prepare():
    archive_path = Path(sys.argv[0]).resolve()
    digest = hashlib.sha256()
    # Keep a single descriptor for hashing and extraction even if the path is replaced.
    with archive_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            manifest = json.loads(archive.read("ZIPTRIP.json"))
            _check_runtime(manifest)
            root = _unpack(archive, digest.hexdigest())
    application = str(root / "app")
    packages = str(root / "site-packages")
    sys.path[:0] = [application, packages]
    # Support wheel-provided .pth files, including namespace-package support.
    site.addsitedir(packages)
    sys.dont_write_bytecode = True
    return manifest["entry_point"], root


def main():
    try:
        entry, root = _prepare()
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile) as error:
        print(f"ziptrip: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    if entry["kind"] == "source":
        runpy.run_path(str(root / "app" / "__main__.py"), run_name="__main__")
    elif entry["kind"] == "module":
        runpy.run_module(entry["value"], run_name="__main__", alter_sys=True)
    else:
        module, attribute = entry["value"].split(":", 1)
        function = importlib.import_module(module)
        for part in attribute.split("."):
            function = getattr(function, part)
        sys.exit(function())


if __name__ == "__main__":
    main()
