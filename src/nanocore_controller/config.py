from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError

_BLUETOOTH_ADDRESS = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
_ADAPTER_NAME = re.compile(r"hci\d+")

# BlueZ names its first adapter hci0, which is also bleak's own default.
DEFAULT_ADAPTER = "hci0"


def detect_adapter(bluetooth_root: str | os.PathLike[str] = "/sys/class/bluetooth") -> str:
    """Return the lowest-numbered BlueZ adapter present, or the default one."""

    try:
        names = [
            entry.name
            for entry in Path(bluetooth_root).iterdir()
            if _ADAPTER_NAME.fullmatch(entry.name)
        ]
    except OSError:
        return DEFAULT_ADAPTER
    return min(names, key=lambda name: int(name[3:])) if names else DEFAULT_ADAPTER


def validate_bluetooth_address(address: object) -> str:
    """Return the upper-cased address, or raise ``ValidationError``."""

    if not isinstance(address, str) or not _BLUETOOTH_ADDRESS.fullmatch(address):
        raise ValidationError("Bluetooth address must contain six colon-separated hex octets")
    return address.upper()


def validate_adapter_name(adapter: object) -> str:
    """Return the adapter name, or raise ``ValidationError``.

    Only BlueZ names (``hci0``, ``hci1``, ...) are accepted, so a value can
    never be mistaken for a command-line option by an external tool.
    """

    if not isinstance(adapter, str) or not adapter:
        raise ValidationError("Bluetooth adapter must be a non-empty string")
    if not _ADAPTER_NAME.fullmatch(adapter):
        raise ValidationError("Bluetooth adapter must look like hci0, hci1, ...")
    return adapter


@dataclass(frozen=True)
class DeviceProfile:
    address: str
    adapter: str = DEFAULT_ADAPTER

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", validate_bluetooth_address(self.address))
        validate_adapter_name(self.adapter)


def default_config_path() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    xdg_config_root = Path(xdg_config_home) if xdg_config_home else None
    config_root = (
        xdg_config_root
        if xdg_config_root is not None and xdg_config_root.is_absolute()
        else Path.home() / ".config"
    )
    return config_root / "nanocore-controller" / "config.json"


def load_profile(path: str | os.PathLike[str] | None = None) -> DeviceProfile:
    config_path = Path(path) if path is not None else default_config_path()
    try:
        contents = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"NanoCore config file not found: {config_path}"
        ) from error
    except UnicodeDecodeError as error:
        raise ValueError(
            f"NanoCore config contains malformed JSON: {config_path}"
        ) from error

    try:
        document: Any = json.loads(contents)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"NanoCore config contains malformed JSON: {config_path}"
        ) from error

    if not isinstance(document, dict):
        raise ValueError("NanoCore config root must be an object")

    expected_keys = {"version", "address", "adapter"}
    if set(document) != expected_keys:
        raise ValueError(
            "NanoCore config schema keys must be exactly: version, address, adapter"
        )

    version = document["version"]
    if type(version) is not int or version != 1:
        raise ValueError("NanoCore config must use exactly version 1")

    try:
        return DeviceProfile(document["address"], document["adapter"])
    except ValueError as error:
        raise ValueError(f"NanoCore config contains an invalid profile: {error}") from error


def save_profile(
    profile: DeviceProfile, path: str | os.PathLike[str] | None = None
) -> None:
    config_path = Path(path) if path is not None else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "address": profile.address,
        "adapter": profile.adapter,
    }
    serialized = json.dumps(document) + "\n"

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, config_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
