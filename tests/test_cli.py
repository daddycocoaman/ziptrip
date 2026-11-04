import json

import pytest
from typer.testing import CliRunner

from ziptrip import __version__
from ziptrip.cli import app

runner = CliRunner()


def test_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["build", "--help"]).exit_code == 0
    assert runner.invoke(app, ["--version"]).stdout.strip() == f"ziptrip {__version__}"


def test_target_json():
    result = runner.invoke(app, ["targets", "--json"])
    assert result.exit_code == 0
    values = json.loads(result.stdout)
    assert any(item["triple"] == "aarch64-unknown-linux-musl" for item in values)
    assert len(values) == 9


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--target", "aarch64-alpine-linux"], "Unsupported target"),
        (["--python-version", "3.9"], "Python version must be"),
        (["--entry-point", "invalid"], "module:function"),
        (["--entry-point", "pkg:main", "--module", "pkg"], "Choose only one"),
        ([], "Choose --entry-point"),
    ],
)
def test_invalid_build(tmp_path, extra, message):
    result = runner.invoke(
        app,
        [
            "build",
            "--target",
            "aarch64-unknown-linux-musl",
            "--python-version",
            "3.12",
            "--output",
            str(tmp_path / "app.pyz"),
            *extra,
        ],
    )
    assert result.exit_code == 1
    assert message in result.output
    assert not (tmp_path / "app.pyz").exists()


def test_build_and_inspect_json(tmp_path):
    output = tmp_path / "app.pyz"
    result = runner.invoke(
        app,
        [
            "build",
            "--target",
            "aarch64-unknown-linux-musl",
            "--python-version",
            "3.12",
            "--module",
            "json.tool",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["inspect", str(output), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["target"]["triple"] == "aarch64-unknown-linux-musl"


def test_invalid_archive(tmp_path):
    output = tmp_path / "bad.pyz"
    output.write_text("not an archive")
    result = runner.invoke(app, ["inspect", str(output)])
    assert result.exit_code == 1
    assert "Cannot inspect" in result.output


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        None,
        {"format_version": 2},
        {
            "format_version": 1,
            "target": {},
            "python_version": "3.12",
            "entry_point": {},
            "distributions": [],
            "native_files": [],
        },
    ],
)
def test_malformed_manifest_is_reported(tmp_path, manifest):
    from zipfile import ZipFile

    output = tmp_path / "bad.pyz"
    with ZipFile(output, "w") as archive:
        archive.writestr("ZIPTRIP.json", json.dumps(manifest))
    result = runner.invoke(app, ["inspect", str(output)])
    assert result.exit_code == 1
    assert "Cannot inspect" in result.output
