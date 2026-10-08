"""Back up the amplifier models and impulse responses stored in the pedal.

Every slot is read from the pedal (read only), checked against its CRC-32 and written to its own file;
a manifest records what is needed to check the files later and to restore a slot.
"""

import hashlib
import json
import os
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import assets_protocol as assets
from .device import NanocoreDevice
from .errors import NanocoreError, ProtocolError, ValidationError
from .safe_files import write_new

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "nanocore-controller-assets"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "slot"


async def backup_assets(
    device: NanocoreDevice,
    directory: Path | str,
    *,
    kinds: Sequence[assets.AssetKind] = (assets.AMP, assets.IR),
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Read every present slot of the given storages into ``directory`` and return the manifest."""

    target = Path(directory)
    if (target / MANIFEST_NAME).exists():
        raise ValidationError(f"{target} already holds a backup; nothing was overwritten")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    items: list[dict[str, Any]] = []
    for kind in kinds:
        for info in await device.list_assets(kind):
            if not info.present:
                continue
            try:
                _, data = await device.read_asset(kind, info.slot)
            except NanocoreError as exc:
                raise ProtocolError(f"{kind.name} {info.slot} ({info.name}): {exc}") from exc
            file_name = f"{kind.name}-{info.slot:02d}-{_safe(info.name)}.bin"
            write_new(target / file_name, data)
            items.append(
                {
                    "kind": kind.name,
                    "slot": info.slot,
                    "name": info.name,
                    "size": len(data),
                    "crc32": assets.crc32(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "file": file_name,
                    "active": info.active,
                }
            )
            if progress is not None:
                progress(f"{kind.name} {info.slot:2d} {info.name} ({len(data)} bytes)")
    manifest = {
        "format": MANIFEST_FORMAT,
        "version": 1,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "items": items,
    }
    write_new(target / MANIFEST_NAME, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return manifest
