import subprocess
import sys
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from ziptrip.builder import BuildError, build_archive
from ziptrip.cli import app
from ziptrip.targets import get_target


def test_build_dot_command_for_alpine(local_project, monkeypatch):
    project, directory = local_project()
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        app,
        [
            "build",
            ".",
            "-e",
            "vortex.entrypoint:main",
            "--target",
            "aarch64-unknown-linux-musl",
            "-p",
            "3.14",
            "--output",
            "dist/vortex-alpine-arm64.pyz",
            "--offline",
            "--no-index",
            "--find-links",
            str(directory),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (project / "backend-executed").exists()
    with ZipFile(project / "dist" / "vortex-alpine-arm64.pyz") as archive:
        assert archive.read("site-packages/vortex/data.txt") == b"bundled data"
        assert "site-packages/vortex/entrypoint.py" in archive.namelist()


@pytest.mark.parametrize("entry", ["function", "console_script"])
def test_local_project_runs_with_metadata_and_data(options, local_project, runtime_env, entry):
    project, directory = local_project()
    options.packages = [str(project)]
    options.find_links = [str(directory)]
    if entry == "function":
        options.entry_point = "vortex.entrypoint:main"
    else:
        options.console_script = "vortex"
    manifest = build_archive(options)
    assert [item["name"] for item in manifest["distributions"]] == ["vortex"]
    result = subprocess.run(
        [sys.executable, "-I", str(options.output)],
        env=runtime_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0.8.0 bundled data\n"


def test_local_dependencies_use_target_markers_and_extras(options, local_project, wheelhouse):
    directory, make = wheelhouse
    tag = "cp314-cp314-musllinux_1_2_aarch64"
    make("native", tag=tag, files={"native.so": tag})
    make("feature-dependency")
    project, _ = local_project(
        requires=[
            "native; platform_machine == 'aarch64' and python_version == '3.14'",
            "missing-windows-dependency; sys_platform == 'win32'",
            "feature-dependency; extra == 'feature'",
        ]
    )
    options.target = get_target("aarch64-unknown-linux-musl")
    options.python_version = "3.14"
    options.packages = [str(project) + "[feature]"]
    options.find_links = [str(directory)]
    options.entry_point = "vortex.entrypoint:main"
    manifest = build_archive(options)
    assert {item["name"] for item in manifest["distributions"]} == {
        "vortex",
        "native",
        "feature-dependency",
    }
    assert manifest["native_files"] == ["site-packages/native.so"]


@pytest.mark.parametrize(
    "files, tag",
    [
        ({}, "cp314-cp314-manylinux_2_28_x86_64"),
        ({"vortex/native.so": b"native binary"}, "py3-none-any"),
        ({"vortex/native": b"\x7fELFbinary"}, "py3-none-any"),
    ],
)
def test_local_native_wheel_is_rejected(options, local_project, files, tag):
    project, directory = local_project(files=files, tag=tag)
    options.packages = [str(project)]
    options.find_links = [str(directory)]
    options.entry_point = "vortex.entrypoint:main"
    with pytest.raises(BuildError, match="is not portable"):
        build_archive(options)
    assert not options.output.exists()


def test_hash_mode_rejects_mutable_project_before_build(options, local_project):
    project, directory = local_project()
    options.packages = [str(project)]
    options.find_links = [str(directory)]
    options.entry_point = "vortex.entrypoint:main"
    options.require_hashes = True
    with pytest.raises(BuildError, match="cannot be combined with --require-hashes"):
        build_archive(options)
    assert not (project / "backend-executed").exists()


def test_missing_build_backend_reports_local_build_failure(options, local_project):
    project, _ = local_project()
    options.packages = [str(project)]
    options.entry_point = "vortex.entrypoint:main"
    with pytest.raises(BuildError, match="Local project wheel build failed"):
        build_archive(options)
    assert not options.output.exists()


def test_local_project_missing_target_dependency_does_not_fall_back(options, local_project):
    project, directory = local_project(requires=["missing-dependency"])
    options.packages = [str(project)]
    options.find_links = [str(directory)]
    options.entry_point = "vortex.entrypoint:main"
    with pytest.raises(BuildError, match="Only prebuilt target wheels"):
        build_archive(options)
    assert not options.output.exists()


def test_local_builds_do_not_embed_temporary_paths(options, local_project):
    project, directory = local_project()
    options.packages = [str(project)]
    options.find_links = [str(directory)]
    options.entry_point = "vortex.entrypoint:main"
    build_archive(options)
    first = options.output.read_bytes()
    with ZipFile(options.output) as archive:
        assert "site-packages/vortex-0.8.0.dist-info/direct_url.json" not in archive.namelist()
        assert b"direct_url.json" not in archive.read("site-packages/vortex-0.8.0.dist-info/RECORD")
    options.force = True
    build_archive(options)
    assert options.output.read_bytes() == first
