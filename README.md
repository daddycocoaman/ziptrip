# ziptrip

Build a self-extracting Python zipapp for another operating system or CPU, using
Rust-style target triples. Bundle your application and its dependencies, including
native extensions, into a single `.pyz` file.

## Install

```sh
uv tool install ziptrip
# or: python -m pip install ziptrip
```

## Usage

- `ziptrip targets`: list the supported Rust target triples and platform baselines.
- `ziptrip build [PACKAGES]...`: resolve dependencies and create an archive.
- `ziptrip inspect APP.pyz`: show the target, entry point, packages and native files.
- `--json` on `targets` and `inspect`: produce machine-readable output.

Choose one entry point:

```sh
# Build a local pure-Python project, including its declared dependencies and data files
ziptrip build . -e myapp.cli:main --target aarch64-unknown-linux-musl \
  -p 3.14 -o dist/myapp-alpine-arm64.pyz

# A function in your source tree (copy the contents of src/ into the application)
ziptrip build -r requirements.txt --source src --entry-point myapp.cli:main \
  --target x86_64-unknown-linux-gnu --python-version 3.12 -o dist/myapp.pyz

# Run a bundled module, like python -m http.server
ziptrip build --module http.server --target x86_64-unknown-linux-gnu \
  --python-version 3.12 -o dist/server.pyz

# Use a dependency's console script
ziptrip build httpie --console-script http --target aarch64-apple-darwin \
  --python-version 3.12 -o dist/http.pyz

# Or use a source directory containing __main__.py without an entry-point option
ziptrip build --source myapp --target x86_64-pc-windows-msvc \
  --python-version 3.12 -o dist/myapp.pyz
```

Requirements files (`-r`) and constraints (`-c`) are repeatable. Use `--find-links`
to supply a local wheelhouse, `--no-index` to disable indexes, and `--offline` to
disable network access. `--index-url` sets the primary index. Use `--require-hashes`
with a fully pinned, hashed requirements file for controlled dependency inputs.
An existing output is replaced only when you pass `--force`.

Local project directories passed as positional arguments (`.`, `./myapp`, or
`'./myapp[extra]'`) are built into wheels using their configured build backend.
The backend runs on the host in an isolated build environment; it may create its
normal build files in the project directory. Build requirements must be available
from the configured index, cache, or `--find-links`, including when using `--offline`.
Ziptrip accepts the resulting wheel only if it is pure Python with `none-any`
tags and contains no native binaries. Its package data, metadata, console scripts,
and declared runtime dependencies are preserved. Runtime dependencies are then
resolved for the target, with dependency source builds disabled. Other source
inputs (sdists, VCS URLs, and project directories in requirements files) remain
unsupported. For `--require-hashes`, prebuild the local wheel and list its hash
in the requirements file instead of passing a project directory.

## How it works and limits

The builder uses **uv** (installed as a dependency) to resolve and install wheels
with the target platform and Python version. Dependency markers are evaluated for
the target. It never imports target extensions on the build host. This is
cross-platform **packaging of precompiled wheels**, not a general C/Rust source
cross-compiler. If a dependency has no compatible wheel, the build fails with
guidance to build a wheel in the target environment and supply it via `--find-links`.
Dependency source builds on the host are disabled. Explicit local projects may be
built on the host, but only their validated pure-Python wheels can be bundled.

Supported targets cover x86_64/aarch64 Linux (glibc and musl), macOS, and Windows,
plus i686 Windows. This is a subset of Rust's targets: a Rust target alone does not
guarantee CPython or third-party wheel support. Glibc targets use a 2.28 baseline,
musl targets use 1.2, and macOS targets use 13.0. Python version is an additional,
required input because it determines the Python ABI. Standard GIL-enabled CPython
3.10–3.14 is supported; PyPy and free-threaded CPython are not currently supported.

The archive extracts application files and wheels to a private, content-addressed
cache before importing them. This is required for `.so`/`.pyd` extensions and also
supports packages that read data files by path. The default cache is
`$XDG_CACHE_HOME/ziptrip` (or `~/.cache/ziptrip`, `%LOCALAPPDATA%/ziptrip` on Windows).
Set `ZIPTRIP_CACHE_DIR` to a writable directory to override it. Complete caches are
published atomically so concurrent first launches are safe. Old cache directories
may be deleted when no application is using them; the next launch recreates them.

Python is not included in the archive; install it on the target system.
The launcher checks the Python version, CPU, OS and Linux libc family before
loading application code. External system libraries required by a wheel still need
to exist on the target. The matching Python **minor** version is required even when
some dependencies use Python's stable ABI. Patch versions may be newer than the
build's requested version. Builds cannot verify native execution on a foreign CPU;
test the artifact on the target or under emulation before distributing it.

`--source` copies a plain source tree; it does not build a local Python project or
infer its dependencies. Supply dependencies explicitly or with `-r pyproject.toml`.
VCS metadata, virtual environments, caches, `dist`, `build`, bytecode, `.env` files,
and previous zipapps are excluded. Symlinks and native binaries in the source tree
are rejected: supply native code as correctly tagged wheels. Native wheel content
and package metadata are preserved. Console entry points are resolved from metadata
without importing the package during the build. Generated wheel scripts with host
shebangs are omitted; invoke the chosen console script through the zipapp instead.

ZIP members have normalized timestamps, ordering and permissions. Equivalent
staged inputs produce identical archive bytes. Unpinned requirements may resolve
to different releases later; the embedded manifest is an inventory, not a lockfile.
No target interpreter, compiler, Docker or QEMU is needed to build from wheels.

See [Python's zipapp caveats](https://docs.python.org/3/library/zipapp.html#caveats)
and [uv's target resolution](https://docs.astral.sh/uv/concepts/resolution/#platform-specific-resolution).
