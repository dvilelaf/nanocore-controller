"""Helpers shared by tests that need realistic device payloads."""


def runtime_payload(
    active_preset: int = 8,
    volume: int = 84,
    chain: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7),
) -> bytes:
    """A runtime snapshot (command 0x63) with eight empty effects."""

    body = bytearray((3, active_preset, volume))
    for _ in range(8):
        body.extend((0, 0, 0))
    body.extend((len(chain), *chain))
    return bytes(body)


import json  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from nanocore_controller.errors import (  # noqa: E402
    DeviceDisconnected,
    DeviceStatusError,
    DeviceTimeout,
)
from nanocore_controller.session import FakeSession  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "backup-v1.json"


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class PedalSession(FakeSession):
    """A stateful stand-in for a pedal that serves one backup document.

    ``log`` records every command in order. ``before_command`` maps a command
    number to a hook run just before that command is answered, which lets a
    test change the pedal at an exact moment (a preset switch, a knob turn).
    ``fail_live_write`` names the numbers of the live writes (1-based) that
    fail once with the given exception.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        super().__init__()
        self.document = document
        self.runtime = bytearray.fromhex(document["raw_snapshot"])
        self.log: list[int] = []
        self.before_command: dict[int, Callable[[PedalSession], None]] = {}
        self.saved_slots: list[int] = []
        self.live_writes: list[bytes] = []
        self.fail_live_write: dict[int, Exception] = {}
        self.fail_all_live_writes_from: int | None = None
        self.save_error: Exception | None = None
        # Global settings as the real pedal answered command 0x65 on 2026-10-08 (input gain +1).
        self.settings = bytearray.fromhex("01 01 00 01 64 64 00")
        self.set_payloads: list[bytes] = []
        self.ignore_settings_writes = False
        self.catalog_names: dict[int, str] = {}
        self.live_name: str | None = None
        self.name_echo: bytes | None = None  # a test can make the pedal answer something else

    def switch_to_slot(self, slot: int) -> None:
        self.runtime[1] = slot

    async def query(self, command, payload=b"", *, sequence=None, timeout=5.0):
        from nanocore_controller.nanocore_protocol import NanocoreResponse

        if not self._connected:
            raise DeviceDisconnected("session is not connected")
        self.queries.append((command, payload))
        self.log.append(command)
        hook = self.before_command.get(command)
        if hook is not None:
            hook(self)
        if command == 0x65:
            return NanocoreResponse(1, command, 0, bytes(self.settings))
        if command == 0x66:
            self.set_payloads.append(payload)
            mask = payload[0]
            values = list(payload[1:])
            fields = [offset for bit, offset in ((0x01, 1), (0x02, 2), (0x04, 3), (0x08, 4), (0x10, 5), (0x20, 6)) if mask & bit]
            if len(values) != len(fields):  # like the real pedal: the length must match the mask
                raise DeviceStatusError(command, 0x01)
            for offset, value in zip(fields, values, strict=True):
                if not self.ignore_settings_writes:
                    self.settings[offset] = value
            return NanocoreResponse(1, command, 0, bytes(self.settings))
        if command == 0x63:
            return NanocoreResponse(1, command, 0, bytes(self.runtime))
        if command == 0x40:
            slot = self.runtime[1]
            shown = self.catalog_names.get(slot, self.document["preset"]["name"] or "")
            name = shown.encode().ljust(8, b" ")[:8]
            return NanocoreResponse(1, command, 0, bytes([(slot // 6) * 6, 1, slot, 0]) + name)
        if command in (0x36, 0x56):
            asset = self.document["assets"]["amp" if command == 0x36 else "ir"]
            if payload[0] != asset["slot"]:
                raise DeviceTimeout(command, maybe_applied=False)
            return NanocoreResponse(1, command, 0, bytes.fromhex(asset["raw"]))
        if command == 0x46:
            if self.save_error is not None:
                raise self.save_error
            self.saved_slots.append(payload[0])
            if self.live_name is not None:
                self.catalog_names[payload[0]] = self.live_name
            return NanocoreResponse(1, command, 0, b"")
        if command == 0x6D:
            self.live_writes.append(payload)
            if payload[:1] == b"\x08":
                self.live_name = payload[2 : 2 + payload[1]].decode()
                return NanocoreResponse(1, command, 0, self.name_echo if self.name_echo is not None else payload)
            number = len(self.live_writes)
            if number in self.fail_live_write:
                raise self.fail_live_write.pop(number)
            if self.fail_all_live_writes_from is not None and number >= self.fail_all_live_writes_from:
                raise DeviceDisconnected("link lost")
            return NanocoreResponse(1, command, 0, b"")
        raise AssertionError(f"PedalSession: unexpected command 0x{command:02x}")


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    """Poll until ``predicate`` is true, so tests do not depend on disk or CPU speed."""

    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
