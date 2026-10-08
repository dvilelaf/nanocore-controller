"""Runtime discovery of NANOCORE USB MIDI and BLE endpoints."""

import asyncio
import re
import subprocess
from collections.abc import Callable

from .errors import DeviceNotFound

_AMIDI_LINE = re.compile(r"^\s*\S+\s+(hw:\S+)\s+(.+?)\s*$", re.IGNORECASE)


def find_nanocore_midi_port(amidi_output: str) -> str:
    """Return the ALSA hardware port for a NANOCORE MIDI endpoint."""

    for line in amidi_output.splitlines():
        match = _AMIDI_LINE.match(line)
        if match and "nanocore midi" in match.group(2).lower():
            return match.group(1)
    raise DeviceNotFound("Nanocore MIDI endpoint was not found")


def discover_nanocore_midi_port(
    executable: str = "amidi",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 5.0,
) -> str:
    """Run amidi discovery and return the current NANOCORE hardware port.

    This blocks for up to ``timeout`` seconds; use the async variant inside an
    event loop.
    """

    try:
        result = runner(
            [executable, "-l"], check=True, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{executable} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeviceNotFound(f"{executable} -l timed out after {timeout:g}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise DeviceNotFound(
            f"{executable} -l failed (exit {exc.returncode})" + (f": {detail}" if detail else "")
        ) from exc
    return find_nanocore_midi_port(result.stdout)


async def discover_nanocore_midi_port_async(
    executable: str = "amidi",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 5.0,
) -> str:
    """``discover_nanocore_midi_port`` run in a worker thread, off the event loop."""

    return await asyncio.to_thread(discover_nanocore_midi_port, executable, runner, timeout)
