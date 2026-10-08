import json
from pathlib import Path

import pytest

from nanocore_controller.config import (
    DEFAULT_ADAPTER,
    DeviceProfile,
    default_config_path,
    detect_adapter,
    load_profile,
    save_profile,
    validate_adapter_name,
    validate_bluetooth_address,
)
from nanocore_controller.errors import ValidationError


def test_device_profile_normalizes_address_and_uses_default_adapter():
    profile = DeviceProfile("aa:bb:cc:dd:ee:ff")

    assert profile.address == "AA:BB:CC:DD:EE:FF"
    assert profile.adapter == DEFAULT_ADAPTER == "hci0"


@pytest.mark.parametrize(
    "address",
    [
        "AA:BB:CC:DD:EE",
        "AA:BB:CC:DD:EE:FF:00",
        "AA-BB-CC-DD-EE-FF",
        "A:BB:CC:DD:EE:FF",
        "GG:BB:CC:DD:EE:FF",
        " AA:BB:CC:DD:EE:FF",
        "AA:BB:CC:DD:EE:FF ",
        "",
        True,
        123,
        None,
    ],
)
def test_device_profile_rejects_invalid_addresses(address):
    with pytest.raises(ValueError, match="Bluetooth address"):
        DeviceProfile(address)  # type: ignore[arg-type]


@pytest.mark.parametrize("adapter", ["", True, 1, None])
def test_device_profile_rejects_invalid_adapters(adapter):
    with pytest.raises(ValueError, match="adapter"):
        DeviceProfile("AA:BB:CC:DD:EE:FF", adapter)  # type: ignore[arg-type]


def test_device_profile_is_immutable():
    profile = DeviceProfile("AA:BB:CC:DD:EE:FF")

    with pytest.raises(AttributeError):
        profile.address = "00:00:00:00:00:00"  # type: ignore[misc]


def test_default_config_path_uses_nonempty_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert default_config_path() == tmp_path / "nanocore-controller" / "config.json"


@pytest.mark.parametrize("xdg_value", [None, "", "relative/config"])
def test_default_config_path_falls_back_to_home(monkeypatch, tmp_path, xdg_value):
    if xdg_value is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg_value)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert default_config_path() == (
        tmp_path / ".config" / "nanocore-controller" / "config.json"
    )


def test_save_and_load_profile_roundtrip(tmp_path):
    path = tmp_path / "nested" / "config.json"
    profile = DeviceProfile("aa:bb:cc:dd:ee:ff", "hci0")

    save_profile(profile, path)

    assert load_profile(path) == profile
    assert path.read_text(encoding="utf-8") == (
        '{"version": 1, "address": "AA:BB:CC:DD:EE:FF", "adapter": "hci0"}\n'
    )


def test_load_profile_uses_default_adapter_from_explicit_schema(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"version": 1, "address": "aa:bb:cc:dd:ee:ff", "adapter": "hci0"}\n',
        encoding="utf-8",
    )

    assert load_profile(path) == DeviceProfile("AA:BB:CC:DD:EE:FF")


def test_load_profile_reports_absent_file_clearly(tmp_path):
    path = tmp_path / "missing.json"

    with pytest.raises(FileNotFoundError, match="NanoCore config file not found"):
        load_profile(path)


def test_load_profile_reports_malformed_json_clearly(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="malformed JSON"):
        load_profile(path)


def test_load_profile_reports_non_utf8_json_clearly(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xff")

    with pytest.raises(ValueError, match="malformed JSON"):
        load_profile(path)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "root must be an object"),
        ({"version": 1, "address": "AA:BB:CC:DD:EE:FF"}, "schema keys"),
        (
            {
                "version": 1,
                "address": "AA:BB:CC:DD:EE:FF",
                "adapter": "hci1",
                "extra": True,
            },
            "schema keys",
        ),
        (
            {"version": "1", "address": "AA:BB:CC:DD:EE:FF", "adapter": "hci1"},
            "version 1",
        ),
        (
            {"version": 2, "address": "AA:BB:CC:DD:EE:FF", "adapter": "hci1"},
            "version 1",
        ),
        (
            {
                "version": True,
                "address": "AA:BB:CC:DD:EE:FF",
                "adapter": "hci1",
            },
            "version 1",
        ),
        (
            {"version": 1, "address": "invalid", "adapter": "hci1"},
            "invalid profile",
        ),
    ],
)
def test_load_profile_rejects_invalid_schema(tmp_path, document, message):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_profile(path)


def test_save_profile_atomically_replaces_existing_config(monkeypatch, tmp_path):
    from nanocore_controller import config

    path = tmp_path / "config.json"
    path.write_text("old\n", encoding="utf-8")
    replacements = []
    real_replace = config.os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", recording_replace)

    save_profile(DeviceProfile("AA:BB:CC:DD:EE:FF"), path)

    assert len(replacements) == 1
    source, destination = replacements[0]
    assert source.parent == path.parent
    assert destination == path
    assert load_profile(path) == DeviceProfile("AA:BB:CC:DD:EE:FF")


def test_save_profile_flushes_and_fsyncs_before_replace(monkeypatch, tmp_path):
    from nanocore_controller import config

    path = tmp_path / "config.json"
    calls = []
    real_fsync = config.os.fsync
    real_replace = config.os.replace

    def recording_fsync(fd):
        calls.append("fsync")
        real_fsync(fd)

    def recording_replace(source, destination):
        calls.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(config.os, "fsync", recording_fsync)
    monkeypatch.setattr(config.os, "replace", recording_replace)

    save_profile(DeviceProfile("AA:BB:CC:DD:EE:FF"), path)

    assert calls == ["fsync", "replace"]


def test_save_profile_removes_temporary_file_when_replace_fails(
    monkeypatch, tmp_path
):
    from nanocore_controller import config

    path = tmp_path / "config.json"
    path.write_text("existing\n", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(config.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_profile(DeviceProfile("AA:BB:CC:DD:EE:FF"), path)

    assert path.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.iterdir()) == [path]


def test_detect_adapter_returns_the_lowest_numbered_adapter(tmp_path):
    for name in ("hci10", "hci2", "hci1", "rfkill0", "notes"):
        (tmp_path / name).mkdir()

    assert detect_adapter(tmp_path) == "hci1"


def test_detect_adapter_falls_back_to_default_without_adapters(tmp_path):
    assert detect_adapter(tmp_path) == DEFAULT_ADAPTER
    assert detect_adapter(tmp_path / "missing") == DEFAULT_ADAPTER


@pytest.mark.parametrize("adapter", ["--help", "hci", "hciX", "eth0", "hci1 ", "../hci0"])
def test_device_profile_rejects_adapters_that_are_not_bluez_names(adapter):
    with pytest.raises(ValueError, match="hci0"):
        DeviceProfile("AA:BB:CC:DD:EE:FF", adapter)


def test_validate_bluetooth_address_returns_the_normalized_address():
    assert validate_bluetooth_address("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"
    for bad in ("AA:BB", None, 5, "--address"):
        with pytest.raises(ValidationError):
            validate_bluetooth_address(bad)


def test_validate_adapter_name_accepts_only_bluez_names():
    assert validate_adapter_name("hci3") == "hci3"
    for bad in ("", None, "-i", "hci", "hci0;"):
        with pytest.raises(ValidationError):
            validate_adapter_name(bad)
