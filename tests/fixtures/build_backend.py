"""Minimal PEP 517 backend for exercising isolated local builds without network access."""

import base64
import hashlib
from pathlib import Path
from zipfile import ZipFile


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    Path("backend-executed").touch()
    tag = Path("wheel-tag.txt").read_text()
    name = f"vortex-0.8.0-{tag}.whl"
    metadata = "vortex-0.8.0.dist-info"
    contents = {
        f"{metadata}/METADATA": Path("PKG-INFO").read_bytes(),
        f"{metadata}/WHEEL": (f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n").encode(),
        f"{metadata}/entry_points.txt": b"[console_scripts]\nvortex = vortex.entrypoint:main\n",
    }
    for path in Path("src").rglob("*"):
        if path.is_file():
            contents[path.relative_to("src").as_posix()] = path.read_bytes()
    records = []
    with ZipFile(Path(wheel_directory) / name, "w") as archive:
        for path, content in contents.items():
            archive.writestr(path, content)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
            records.append(f"{path},sha256={digest},{len(content)}")
        record = f"{metadata}/RECORD"
        archive.writestr(record, "\n".join([*records, f"{record},,"]))
    return name
