"""Backward-compatible Bluetooth entry point; new code should use ``NanocoreDevice``."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .ble_midi import BleMidiSession
from .config import DEFAULT_ADAPTER
from .device import NanocoreDevice
from .session import SysexSession


class BluetoothController(NanocoreDevice):
    """A ``NanocoreDevice`` over one BLE-MIDI session, with the old method names."""

    def __init__(
        self,
        address: str,
        *,
        adapter: str = DEFAULT_ADAPTER,
        session_factory: Callable[..., SysexSession] = BleMidiSession,
        preset_readback_delay: float = 0.05,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            session_factory(address, adapter=adapter),
            device_info={"address": address, "adapter": adapter},
            preset_readback_delay=preset_readback_delay,
            sleep=sleep,
        )
        self.address = address
        self.adapter = adapter

    async def status(self) -> dict[str, Any]:
        return await self.read_state()

    async def presets(self) -> list[dict[str, Any]]:
        return await self.catalog()
