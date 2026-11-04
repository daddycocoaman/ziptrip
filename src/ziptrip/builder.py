"""Resolve foreign wheels, stage source, and write a deterministic zipapp."""

from __future__ import annotations

import csv
import json
import keyword
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from email.parser import Parser
from importlib import metadata
from pathlib import Path

from ziptrip import __version__
from ziptrip.targets import Target, resolver_platform


class BuildError(Exception):
    """An actionable error that can be displayed without a traceback."""


@dataclass
class BuildOptions:
    target: Target
    python_version: str
    output: Path
    packages: list[str] = field(default_factory=list)
    requirements: list[Path] = field(default_factory=list)
    constraints: list[Path] = field(default_factory=list)
    source: Path | None = None
    entry_point: str | None = None
    module: str | None = None
    console_script: str | None = None
    find_links: list[str] = field(default_factory=list)
    index_url: str | None = None
    no_index: bool = False
    offline: bool = False
    require_hashes: bool = False
    force: bool = False


EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".ziptrip-cache",
    "dist",
    "build",
}


def is_native(path: Path) -> bool:
    return bool(re.search(r"\.(?:so(?:\.\d+)*|pyd|dll|dylib|exe|a|lib|o)$", path.name, re.I))


def _dotted_name(value: str) -> bool:
    return bool(value) and all(
        part.isidentifier() and not keyword.iskeyword(part) for part in value.split(".")
    )


def validate_function(value: str) -> None:
    module, separator, function = value.partition(":")
    if not separator or not _dotted_name(module) or not _dotted_name(function):
        raise BuildError("Entry point must be 'module:function', e.g. 'myapp.cli:main'.")


def validate_options(options: BuildOptions) -> None:
    if not re.fullmatch(r"3\.(?:10|11|12|13|14)(?:\.(?:0|[1-9][0-9]*))?", options.python_version):
        raise BuildError("Python version must be 3.10–3.14, optionally with a patch (e.g. 3.12.4).")
    if (
        sum(bool(value) for value in (options.entry_point, options.module, options.console_script))
        > 1
    ):
        raise BuildError("Choose only one of --entry-point, --module, or --console-script.")
    if options.entry_point:
        validate_function(options.entry_point)
    if options.module and not _dotted_name(options.module):
        raise BuildError("Module must be a dotted Python module name, e.g. 'myapp'.")
    if options.source is not None and not options.source.is_dir():
        raise BuildError(f"Source is not a directory: {options.source}")
    for path in options.requirements + options.constraints:
        if not path.is_file():
            raise BuildError(f"Requirements/constraints file not found: {path}")
    if not any((options.entry_point, options.module, options.console_script)):
        if options.source is None or not (options.source / "__main__.py").is_file():
            raise BuildError(
                "Choose --entry-point, --module, or --console-script, "
                "or supply a --source directory containing __main__.py."
            )
    if options.output.is_symlink():
        raise BuildError(f"Output must not be a symlink: {options.output}")
    if options.output.exists() and (not options.force or not options.output.is_file()):
        raise BuildError(f"Output already exists: {options.output}. Use --force to replace a file.")
    if options.source and options.output.resolve().is_relative_to(options.source.resolve()):
        raise BuildError(
            "Place --output outside the --source directory to avoid copying the output."
        )
    if any(value.startswith("-") or "\n" in value or "\r" in value for value in options.packages):
        raise BuildError("Packages must be requirement specifiers, not resolver options.")


def _uv_command() -> list[str]:
    # Prefer the declared dependency to an unrelated uv on PATH.
    try:
        import uv
    except ImportError:
        executable = shutil.which("uv")
        if executable:
            return [executable]
        raise BuildError(
            "uv is required. Install ziptrip with its dependencies, or install uv."
        ) from None
    return [str(uv.find_uv_bin())]


def _index_options(options: BuildOptions) -> list[str]:
    arguments = []
    for location in options.find_links:
        arguments.extend(["--find-links", location])
    if options.index_url:
        arguments.extend(["--index-url", options.index_url])
    if options.no_index:
        arguments.append("--no-index")
    if options.offline:
        arguments.append("--offline")
    return arguments


def _local_project(requirement: str) -> tuple[Path, str] | None:
    """Recognize positional project directories, including ./project[extras]."""
    path = Path(requirement).expanduser()
    extras = ""
    if not path.is_dir():
        match = re.fullmatch(r"(.+)\[([A-Za-z0-9_., -]+)\]", requirement)
        if match is None:
            return None
        path = Path(match[1]).expanduser()
        if not path.is_dir():
            return None
        extras = "[" + ",".join(part.strip() for part in match[2].split(",")) + "]"
    if not any((path / name).is_file() for name in ("pyproject.toml", "setup.py")):
        raise BuildError(f"Local project needs pyproject.toml or setup.py: {path}")
    return path.resolve(), extras


def _validate_local_wheel(wheel: Path, project: Path) -> None:
    """Only portable pure-Python output from a host build can enter a foreign app."""
    problem = None
    try:
        _, _, abi, platform = wheel.stem.rsplit("-", 3)
        with zipfile.ZipFile(wheel) as archive:
            wheel_metadata = [
                name
                for name in archive.namelist()
                if len(Path(name).parts) == 2 and name.endswith(".dist-info/WHEEL")
            ]
            if len(wheel_metadata) != 1:
                raise ValueError("expected one .dist-info/WHEEL file")
            headers = Parser().parsestr(archive.read(wheel_metadata[0]).decode("utf-8"))
            tags = headers.get_all("Tag", [])
            if (
                abi != "none"
                or platform != "any"
                or headers.get("Root-Is-Purelib", "").lower() != "true"
                or not tags
                or any(not tag.endswith("-none-any") for tag in tags)
            ):
                problem = "the wheel has platform-specific or ABI-specific tags"
            # Catch binaries accidentally shipped as data in a wheel tagged as pure Python.
            for member in archive.infolist():
                if member.is_dir():
                    continue
                with archive.open(member) as stream:
                    magic = stream.read(4)
                if (
                    is_native(Path(member.filename))
                    or magic
                    in {
                        b"\x7fELF",
                        b"\xfe\xed\xfa\xce",
                        b"\xce\xfa\xed\xfe",
                        b"\xfe\xed\xfa\xcf",
                        b"\xcf\xfa\xed\xfe",
                        b"\xca\xfe\xba\xbe",
                        b"\xbe\xba\xfe\xca",
                        b"\xca\xfe\xba\xbf",
                        b"\xbf\xba\xfe\xca",
                    }
                    or magic.startswith(b"MZ")
                ):
                    problem = f"the wheel contains native code: {member.filename}"
                    break
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        raise BuildError(f"Invalid local wheel from {project}: {error}") from error
    if problem:
        raise BuildError(
            f"Local project {project} is not portable: {problem}. "
            "Only pure-Python local projects can be built on the host. Build native projects "
            "for the target separately and pass the resulting wheel or use --find-links."
        )


def _prepare_packages(options: BuildOptions, workspace: Path) -> tuple[list[str], set[str]]:
    packages = []
    wheels: dict[Path, Path] = {}
    for requirement in options.packages:
        local = _local_project(requirement)
        if local is None:
            packages.append(requirement)
            continue
        project, extras = local
        if options.require_hashes:
            raise BuildError(
                "Local project builds cannot be combined with --require-hashes. "
                "Build the project wheel first and list its hash in your requirements file."
            )
        if project not in wheels:
            output = workspace / f"local-wheel-{len(wheels)}"
            command = [
                *_uv_command(),
                "build",
                str(project),
                "--wheel",
                "--out-dir",
                str(output),
                "--python",
                sys.executable,
                "--no-python-downloads",
                "--no-config",
                "--no-sources",
                "--no-progress",
                "--color",
                "never",
                *_index_options(options),
            ]
            try:
                result = subprocess.run(command, capture_output=True, text=True)
            except OSError as error:
                raise BuildError(f"Cannot build local project {project}: {error}") from error
            if result.returncode:
                raise BuildError(
                    f"Local project wheel build failed for {project}:\n"
                    f"{result.stderr.strip() or result.stdout.strip()}\n\n"
                    "The project's build backend runs on the host. Its build requirements "
                    "must be available via the configured index, cache, or --find-links."
                )
            artifacts = list(output.glob("*.whl"))
            if len(artifacts) != 1:
                raise BuildError(
                    f"Expected one wheel from local project {project}; found {len(artifacts)}."
                )
            wheel = artifacts[0]
            _validate_local_wheel(wheel, project)
            wheels[project] = wheel
        wheel = wheels[project]
        # Direct references preserve extras and avoid resolving a namesake from an index.
        packages.append(f"{wheel.name.split('-')[0]}{extras} @ {wheel.as_uri()}")
    return packages, {wheel.as_uri() for wheel in wheels.values()}


def _remove_temporary_provenance(destination: Path, local_urls: set[str]) -> None:
    """Do not embed temporary build paths in the application or its RECORD entries."""
    if not local_urls:
        return
    for provenance in destination.glob("*.dist-info/direct_url.json"):
        if json.loads(provenance.read_text())["url"] not in local_urls:
            continue
        record = provenance.with_name("RECORD")
        temporary_files = (provenance, provenance.with_name("uv_cache.json"))
        relative = {path.relative_to(destination).as_posix() for path in temporary_files}
        with record.open(newline="", encoding="utf-8") as stream:
            rows = [row for row in csv.reader(stream) if row[0] not in relative]
        with record.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream, lineterminator="\n").writerows(rows)
        for path in temporary_files:
            path.unlink(missing_ok=True)


def install_dependencies(options: BuildOptions, destination: Path) -> None:
    destination.mkdir(parents=True)
    if not options.packages and not options.requirements:
        return
    packages, local_urls = _prepare_packages(options, destination.parent)
    command = [
        *_uv_command(),
        "pip",
        "install",
        "--target",
        str(destination),
        "--python",
        sys.executable,
        "--python-version",
        options.python_version,
        "--python-platform",
        resolver_platform(options.target),
        "--no-build",
        "--no-config",
        "--no-sources",
        "--no-python-downloads",
        "--link-mode",
        "copy",
        "--no-progress",
        "--color",
        "never",
    ]
    for path in options.requirements:
        command.extend(["--requirements", str(path.resolve())])
    for path in options.constraints:
        command.extend(["--constraints", str(path.resolve())])
    command.extend(_index_options(options))
    if options.require_hashes:
        command.append("--require-hashes")
    command.extend(packages)
    environment = os.environ.copy()
    environment["MACOSX_DEPLOYMENT_TARGET"] = "13.0"
    environment["UV_COMPILE_BYTECODE"] = "0"
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
    except OSError as error:
        raise BuildError(f"Cannot run uv: {error}") from error
    if result.returncode:
        raise BuildError(
            f"Wheel resolution failed for {options.target.triple} / CPython "
            f"{options.python_version}:\n{result.stderr.strip() or result.stdout.strip()}\n\n"
            "Only prebuilt target wheels are accepted for dependencies. "
            "If a wheel is unavailable, build it "
            "in the target environment and provide it with --find-links WHEELHOUSE."
        )
    _remove_temporary_provenance(destination, local_urls)


def copy_source(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for current, directories, files in os.walk(source, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in EXCLUDED_DIRS and not name.endswith(".egg-info")
        )
        base = Path(current)
        for name in directories:
            if (base / name).is_symlink():
                raise BuildError(f"Source symlinks are not supported: {base / name}")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo", ".pyz")) or name.startswith(".env"):
                continue
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise BuildError(f"Source must contain regular files, not symlinks: {path}")
            if is_native(path):
                raise BuildError(f"Supply native code as a target wheel, not --source: {path}")
            output = destination / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)


def inventory(packages: Path) -> list[dict]:
    result = []
    for distribution in metadata.distributions(path=[str(packages)]):
        wheel = Parser().parsestr(distribution.read_text("WHEEL") or "")
        result.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "wheel_tags": wheel.get_all("Tag", []),
            }
        )
    return sorted(result, key=lambda item: item["name"].lower())


def choose_entry_point(options: BuildOptions, packages: Path) -> dict:
    if options.console_script:
        matches = [
            entry
            for distribution in metadata.distributions(path=[str(packages)])
            for entry in distribution.entry_points
            if entry.group == "console_scripts" and entry.name == options.console_script
        ]
        if len(matches) != 1:
            raise BuildError(
                f"Expected one console script named {options.console_script!r}; "
                f"found {len(matches)} in the bundled packages."
            )
        # Old entry points permit an extras suffix; request dependencies explicitly.
        value = matches[0].value.split("[", 1)[0].strip()
        validate_function(value)
        return {"kind": "function", "value": value, "console_script": options.console_script}
    if options.entry_point:
        return {"kind": "function", "value": options.entry_point}
    if options.module:
        return {"kind": "module", "value": options.module}
    return {"kind": "source", "value": "__main__.py"}


def _package_files(root: Path):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] in {"bin", "Scripts"} or "__pycache__" in relative.parts:
            continue
        if path.suffix in {".pyc", ".pyo"} or path.name == ".lock":
            continue
        if path.is_symlink():
            raise BuildError(f"Unexpected symlink in installed wheel: {relative}")
        if path.is_file():
            yield path, "site-packages/" + relative.as_posix()


def _write_member(archive: zipfile.ZipFile, name: str, data: bytes, executable=False) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data, compresslevel=9)


def build_archive(options: BuildOptions) -> dict:
    validate_options(options)
    with tempfile.TemporaryDirectory(prefix="ziptrip-build-") as temporary:
        workspace = Path(temporary)
        packages = workspace / "site-packages"
        install_dependencies(options, packages)
        files = list(_package_files(packages))
        if options.source:
            source = workspace / "app"
            copy_source(options.source, source)
            files.extend(
                (path, "app/" + path.relative_to(source).as_posix())
                for path in sorted(source.rglob("*"))
                if path.is_file()
            )
        manifest = {
            "format_version": 1,
            "ziptrip_version": __version__,
            "target": options.target.as_dict(),
            "python_version": options.python_version,
            "entry_point": choose_entry_point(options, packages),
            "distributions": inventory(packages),
            "native_files": [name for path, name in files if is_native(path)],
        }
        options.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".ziptrip-", dir=options.output.parent)
        pending = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                version = ".".join(options.python_version.split(".")[:2])
                stream.write(f"#!/usr/bin/env python{version}\n".encode())
                with zipfile.ZipFile(stream, "w") as archive:
                    _write_member(
                        archive,
                        "__main__.py",
                        Path(__file__).with_name("_bootstrap.py").read_bytes(),
                    )
                    _write_member(
                        archive,
                        "ZIPTRIP.json",
                        json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n",
                    )
                    for path, archive_name in sorted(files, key=lambda item: item[1]):
                        _write_member(
                            archive,
                            archive_name,
                            path.read_bytes(),
                            bool(path.stat().st_mode & 0o111),
                        )
            pending.chmod(0o755)
            if options.force:
                os.replace(pending, options.output)
            else:
                # Atomic publish without replacing a concurrently created output.
                os.link(pending, options.output)
        finally:
            pending.unlink(missing_ok=True)
    return manifest


def inspect_archive(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            # Archives built before the rename contain their original manifest and launcher.
            name = "ZIPTRIP.json" if "ZIPTRIP.json" in archive.namelist() else "CROSSAPP.json"
            manifest = json.loads(archive.read(name))
        if manifest["format_version"] != 1:
            raise ValueError("unsupported archive format version")
        for key in ("target", "python_version", "entry_point", "distributions", "native_files"):
            if key not in manifest:
                raise ValueError(f"missing manifest field: {key}")
        for key in ("triple", "baseline", "system", "machine"):
            if not isinstance(manifest["target"][key], str):
                raise ValueError(f"invalid target field: {key}")
        if not isinstance(manifest["python_version"], str):
            raise ValueError("invalid Python version")
        entry = manifest["entry_point"]
        if entry["kind"] not in {"source", "module", "function"} or not isinstance(
            entry["value"], str
        ):
            raise ValueError("invalid entry point")
        if not isinstance(manifest["distributions"], list):
            raise ValueError("invalid distributions list")
        for item in manifest["distributions"]:
            if not isinstance(item["name"], str) or not isinstance(item["version"], str):
                raise ValueError("invalid distribution")
            if not isinstance(item["wheel_tags"], list) or not all(
                isinstance(tag, str) for tag in item["wheel_tags"]
            ):
                raise ValueError("invalid wheel tags")
        if not isinstance(manifest["native_files"], list) or not all(
            isinstance(name, str) for name in manifest["native_files"]
        ):
            raise ValueError("invalid native files")
        return manifest
    except (OSError, KeyError, ValueError, TypeError, zipfile.BadZipFile) as error:
        raise BuildError(f"Cannot inspect {path}: {error}") from error
