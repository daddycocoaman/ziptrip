"""Explicit, portable targets understood by the wheel resolver."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Target:
    triple: str
    system: str
    machine: str
    baseline: str
    libc: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


# Keep this list explicit: Rust supports many platforms without a CPython wheel ecosystem.
# Baselines are fixed below rather than relying on host-dependent resolver defaults.
TARGETS = {
    target.triple: target
    for target in (
        Target("aarch64-unknown-linux-musl", "linux", "aarch64", "musl 1.2+", "musl"),
        Target("x86_64-unknown-linux-musl", "linux", "x86_64", "musl 1.2+", "musl"),
        Target("aarch64-unknown-linux-gnu", "linux", "aarch64", "glibc 2.28+", "glibc"),
        Target("x86_64-unknown-linux-gnu", "linux", "x86_64", "glibc 2.28+", "glibc"),
        Target("aarch64-apple-darwin", "darwin", "aarch64", "macOS 13+"),
        Target("x86_64-apple-darwin", "darwin", "x86_64", "macOS 13+"),
        Target("aarch64-pc-windows-msvc", "win32", "aarch64", "Windows ARM64"),
        Target("x86_64-pc-windows-msvc", "win32", "x86_64", "Windows x64"),
        Target("i686-pc-windows-msvc", "win32", "i686", "Windows x86"),
    )
}


def get_target(triple: str) -> Target:
    try:
        return TARGETS[triple]
    except KeyError:
        raise ValueError(
            f"Unsupported target {triple!r}. Run 'ziptrip targets' to see supported Rust triples."
        ) from None


def resolver_platform(target: Target) -> str:
    if target.libc == "glibc":
        return f"{target.machine}-manylinux_2_28"
    return target.triple
