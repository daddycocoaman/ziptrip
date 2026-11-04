import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from ziptrip.builder import BuildError, build_archive, inspect_archive
from ziptrip.targets import get_target


def run_app(path, env, *args):
    return subprocess.run(
        [sys.executable, "-I", str(path), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_source_function_arguments_data_and_exit_status(options, tmp_path, runtime_env):
    source = tmp_path / "source"
    source.mkdir()
    (source / "demo.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "def main():\n"
        "    print(Path(__file__).with_name('message.txt').read_text(), sys.argv[1])\n"
        "    return 7\n"
    )
    (source / "message.txt").write_text("hello")
    options.source = source
    options.entry_point = "demo:main"
    build_archive(options)
    result = run_app(options.output, runtime_env, "world")
    assert result.returncode == 7, result.stderr
    assert result.stdout == "hello world\n"
    assert options.output.read_bytes().startswith(b"#!/usr/bin/env python")


@pytest.mark.parametrize("entry", ["source", "module"])
def test_main_entry_modes(options, tmp_path, runtime_env, entry):
    source = tmp_path / "source"
    source.mkdir()
    name = "__main__.py" if entry == "source" else "demo.py"
    (source / name).write_text("import sys\nprint(__name__, sys.argv[1])\n")
    options.source = source
    options.module = "demo" if entry == "module" else None
    build_archive(options)
    result = run_app(options.output, runtime_env, "forwarded")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "__main__ forwarded\n"


def test_console_script_installs_wheel_data_and_metadata(options, wheelhouse, runtime_env):
    directory, make = wheelhouse
    make(
        "greeting",
        files={
            "greeting/__init__.py": (
                "from importlib.metadata import version\nfrom pathlib import Path\n"
                "def main():\n"
                "    print(version('greeting'), Path(__file__).with_name('data.txt').read_text())\n"
            ),
            "greeting/data.txt": "wheel data",
        },
        scripts={"greet": "greeting:main"},
    )
    options.packages = ["greeting"]
    options.find_links = [str(directory)]
    options.console_script = "greet"
    manifest = build_archive(options)
    assert manifest["entry_point"]["value"] == "greeting:main"
    result = run_app(options.output, runtime_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1.0 wheel data\n"
    with ZipFile(options.output) as archive:
        assert not any(name.startswith("site-packages/bin/") for name in archive.namelist())


def test_foreign_wheels_and_target_dependency_markers(options, wheelhouse):
    directory, make = wheelhouse
    for tag in ("cp312-cp312-musllinux_1_2_aarch64", "cp312-cp312-manylinux_2_28_x86_64"):
        make("native", tag=tag, files={"native.so": tag})
    make("armdep")
    make("windep")
    make("py312dep")
    make("py314dep")
    make(
        "application",
        requires=[
            "native",
            "armdep; platform_machine == 'aarch64'",
            "windep; sys_platform == 'win32'",
            "py312dep; python_version == '3.12'",
            "py314dep; python_version == '3.14'",
        ],
        files={"application.py": "raise RuntimeError('must not import during build')\n"},
        scripts={"application": "application:main"},
    )
    options.target = get_target("aarch64-unknown-linux-musl")
    options.python_version = "3.12"
    options.packages = ["application"]
    options.console_script = "application"
    options.find_links = [str(directory)]
    manifest = build_archive(options)
    names = {item["name"] for item in manifest["distributions"]}
    assert names == {"application", "native", "armdep", "py312dep"}
    with ZipFile(options.output) as archive:
        assert archive.read("site-packages/native.so") == b"cp312-cp312-musllinux_1_2_aarch64"
    assert manifest["native_files"] == ["site-packages/native.so"]


def test_missing_target_wheel_fails_without_output(options, wheelhouse):
    directory, make = wheelhouse
    make("native", tag="cp312-cp312-manylinux_2_28_x86_64")
    options.target = get_target("aarch64-unknown-linux-musl")
    options.python_version = "3.12"
    options.packages = ["native"]
    options.module = "native"
    options.find_links = [str(directory)]
    with pytest.raises(BuildError, match="Only prebuilt target wheels"):
        build_archive(options)
    assert not options.output.exists()


def test_source_build_never_executes_backend(options, tmp_path):
    import tarfile

    project = tmp_path / "sdist"
    project.mkdir()
    marker = tmp_path / "backend-executed"
    (project / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
    )
    (project / "backend.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    sdist = tmp_path / "untrusted-1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(project, arcname="untrusted-1.0")
    options.packages = [str(sdist)]
    options.module = "anything"
    with pytest.raises(BuildError, match="Wheel resolution failed"):
        build_archive(options)
    assert not marker.exists()


def test_requirements_constraints_and_hashes(options, tmp_path, wheelhouse):
    import hashlib

    directory, make = wheelhouse
    wheel = make("greeting", version="1.0")
    make("greeting", version="2.0")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        f"greeting==1.0 --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n"
    )
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("greeting==1.0\n")
    options.requirements = [requirements]
    options.constraints = [constraints]
    options.find_links = [str(directory)]
    options.require_hashes = True
    options.module = "greeting"
    assert build_archive(options)["distributions"][0]["version"] == "1.0"
    requirements.write_text("greeting==1.0 --hash=sha256:" + "0" * 64 + "\n")
    options.output = tmp_path / "bad.pyz"
    with pytest.raises(BuildError, match="Wheel resolution failed"):
        build_archive(options)


def test_deterministic_build(options, tmp_path, wheelhouse):
    directory, make = wheelhouse
    make("greeting")
    options.packages = ["greeting"]
    options.find_links = [str(directory)]
    options.module = "greeting"
    build_archive(options)
    first = options.output.read_bytes()
    options.output = tmp_path / "second.pyz"
    build_archive(options)
    assert options.output.read_bytes() == first


def test_output_preserved_on_failure_and_force_replaces(options):
    options.output.write_bytes(b"existing")
    options.module = "json.tool"
    with pytest.raises(BuildError, match="already exists"):
        build_archive(options)
    options.force = True
    options.packages = ["nonexistent-ziptrip-test-package"]
    with pytest.raises(BuildError):
        build_archive(options)
    assert options.output.read_bytes() == b"existing"
    options.packages = []
    build_archive(options)
    assert inspect_archive(options.output)["entry_point"]["value"] == "json.tool"


def test_source_filters(options, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "__main__.py").write_text("print('hello')")
    for directory in (".venv", ".git", "__pycache__", "dist"):
        (source / directory).mkdir()
        (source / directory / "skip.py").write_text("ignored")
    (source / ".env").write_text("ignored")
    options.source = source
    build_archive(options)
    with ZipFile(options.output) as archive:
        assert [name for name in archive.namelist() if name.startswith("app/")] == [
            "app/__main__.py"
        ]


@pytest.mark.parametrize("kind", ["symlink", "native"])
def test_source_rejects_nonportable_files(options, tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    (source / "__main__.py").write_text("pass")
    options.source = source
    if kind == "symlink":
        if os.name == "nt":
            pytest.skip("Creating symlinks requires Windows privileges")
        (source / "linked").symlink_to(source / "__main__.py")
    else:
        (source / "native.so").write_text("host extension")
    with pytest.raises(BuildError):
        build_archive(options)


def test_wrong_runtime_fails_before_extracting(options, runtime_env):
    options.module = "json.tool"
    options.python_version = "3.10" if sys.version_info.minor != 10 else "3.11"
    build_archive(options)
    result = run_app(options.output, runtime_env)
    assert result.returncode == 1
    assert "requires GIL-enabled CPython" in result.stderr
    assert not Path(runtime_env["ZIPTRIP_CACHE_DIR"]).exists()


def test_concurrent_first_launch_and_cache_reuse(options, tmp_path, runtime_env):
    source = tmp_path / "source"
    source.mkdir()
    (source / "__main__.py").write_text("print('works')\n")
    options.source = source
    build_archive(options)
    processes = [
        subprocess.Popen(
            [sys.executable, "-I", str(options.output)],
            env=runtime_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert stdout == "works\n"
    root = Path(runtime_env["ZIPTRIP_CACHE_DIR"])
    entries = list(root.iterdir())
    assert len(entries) == 1
    cached_file = entries[0] / "app" / "__main__.py"
    before = cached_file.stat().st_mtime_ns
    assert run_app(options.output, runtime_env).returncode == 0
    assert cached_file.stat().st_mtime_ns == before


def test_inspect_does_not_execute_source(options, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "__main__.py").write_text("raise RuntimeError('do not execute')")
    options.source = source
    manifest = build_archive(options)
    assert inspect_archive(options.output) == manifest
    with ZipFile(options.output) as archive:
        assert json.loads(archive.read("ZIPTRIP.json")) == manifest


def test_inspect_archive_from_before_rename(options, tmp_path):
    options.module = "json.tool"
    manifest = build_archive(options)
    manifest["crossapp_version"] = manifest.pop("ziptrip_version")
    old_archive = tmp_path / "legacy.pyz"
    with ZipFile(old_archive, "w") as archive:
        archive.writestr("CROSSAPP.json", json.dumps(manifest))
    assert inspect_archive(old_archive) == manifest
