"""Safe access to the zip container of a .pbix / .pbit.

A rewrite copies every part byte-for-byte except the one being edited, keeping order,
timestamps and compression, so the compressed model is never recompressed or altered."""
from __future__ import annotations

import zipfile
from pathlib import Path


def list_members(path: Path) -> list[dict]:
    try:
        with zipfile.ZipFile(path) as zf:
            return [{"name": i.filename, "bytes": i.file_size} for i in zf.infolist()]
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path.name} is not a valid Power BI zip container.") from exc


def read_member(path: Path, member: str) -> bytes:
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.read(member)
    except KeyError as exc:
        raise ValueError(f"{path.name} has no part named {member!r}. Use review_file to list parts.") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path.name} is not a valid Power BI zip container.") from exc


def rewrite_member(path: Path, member: str, new_bytes: bytes, out_path: Path) -> None:
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(out_path, "w") as zout:
        if member not in zin.namelist():
            raise ValueError(f"{path.name} has no part named {member!r}.")
        for info in zin.infolist():
            data = new_bytes if info.filename == member else zin.read(info.filename)
            zout.writestr(info, data, compress_type=info.compress_type)


def checksums(path: Path) -> dict[str, int]:
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad:
            raise ValueError(f"Corrupt part in container: {bad}")
        return {i.filename: i.CRC for i in zf.infolist()}
