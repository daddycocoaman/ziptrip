import base64
import hashlib
import os
import platform
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from ziptrip.builder import BuildOptions
from ziptrip.targets import get_target


@pytest.fixture(autouse=True)
def private_uv_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))


@pytest.fixture
def host_target():
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    if sys.platform == "linux":
        from ziptrip._bootstrap import _linux_libc

        suffix = "unknown-linux-musl" if _linux_libc()[0] == "musl" else "unknown-linux-gnu"
    elif sys.platform == "darwin":
        suffix = "apple-darwin"
    elif sys.platform == "win32":
        suffix = "pc-windows-msvc"
    else:
        pytest.skip("Host is not a supported execution target")
    return get_target(f"{machine}-{suffix}")


@pytest.fixture
def options(tmp_path, host_target):
    return BuildOptions(
        target=host_target,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        output=tmp_path / "app.pyz",
        no_index=True,
        offline=True,
    )


@pytest.fixture
def wheelhouse(tmp_path):
    directory = tmp_path / "wheels"
    directory.mkdir()

    def make(name, *, version="1.0", tag="py3-none-any", files=None, requires=(), scripts=None):
        normalized = name.replace("-", "_")
        dist_info = f"{normalized}-{version}.dist-info"
        contents = dict(files or {f"{normalized}/__init__.py": ""})
        contents[f"{dist_info}/METADATA"] = (
            f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\nRequires-Python: >=3.10\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
            + "\n"
        )
        contents[f"{dist_info}/WHEEL"] = (
            "Wheel-Version: 1.0\nGenerator: ziptrip-tests\n"
            f"Root-Is-Purelib: {'true' if tag.endswith('-any') else 'false'}\nTag: {tag}\n\n"
        )
        if scripts:
            contents[f"{dist_info}/entry_points.txt"] = "[console_scripts]\n" + "".join(
                f"{name} = {value}\n" for name, value in scripts.items()
            )
        records = []
        output = directory / f"{normalized}-{version}-{tag}.whl"
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for path, content in contents.items():
                data = content.encode() if isinstance(content, str) else content
                archive.writestr(path, data)
                digest = (
                    base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
                )
                records.append(f"{path},sha256={digest},{len(data)}")
            record = f"{dist_info}/RECORD"
            archive.writestr(record, "\n".join([*records, f"{record},,"]) + "\n")
        return output

    return directory, make


@pytest.fixture
def runtime_env(tmp_path):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["ZIPTRIP_CACHE_DIR"] = str(tmp_path / "runtime-cache")
    return environment


@pytest.fixture
def local_project(tmp_path, wheelhouse):
    directory, make = wheelhouse
    backend = Path(__file__).with_name("fixtures") / "build_backend.py"
    make("ziptrip-test-backend", files={"ziptrip_test_backend.py": backend.read_text()})

    def create(*, tag="py3-none-any", requires=(), files=None):
        project = tmp_path / "local project"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["ziptrip-test-backend==1.0"]\n'
            'build-backend = "ziptrip_test_backend"\n'
        )
        (project / "PKG-INFO").write_text(
            "Metadata-Version: 2.3\nName: vortex\nVersion: 0.8.0\n"
            "Requires-Python: >=3.10\nProvides-Extra: feature\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
            + "\n"
        )
        (project / "wheel-tag.txt").write_text(tag)
        contents = {
            "vortex/__init__.py": "",
            "vortex/entrypoint.py": (
                "from pathlib import Path\nfrom importlib.metadata import version\n"
                "def main():\n"
                "    print(version('vortex'), Path(__file__).with_name('data.txt').read_text())\n"
            ),
            "vortex/data.txt": "bundled data",
            **(files or {}),
        }
        for name, content in contents.items():
            path = project / "src" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode() if isinstance(content, str) else content)
        return project, directory

    return create
