import asyncio
import subprocess
import threading
import unittest

import pytest

from nanocore_controller.discovery import (
    discover_nanocore_midi_port,
    discover_nanocore_midi_port_async,
    find_nanocore_midi_port,
)
from nanocore_controller.errors import DeviceNotFound


class DiscoveryTests(unittest.TestCase):
    def test_finds_nanocore_port_without_assuming_card_number(self):
        output = """Dir Device    Name
IO  hw:2,0,0  Other MIDI
IO  hw:6,0,0  Nanocore MIDI 1
"""
        self.assertEqual(find_nanocore_midi_port(output), "hw:6,0,0")

    def test_missing_nanocore_has_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, "Nanocore MIDI"):
            find_nanocore_midi_port("Dir Device Name\nIO hw:2,0,0 Other MIDI\n")


LISTING = "Dir Device    Name\nIO  hw:6,0,0  Nanocore MIDI 1\n"


def completed(stdout=LISTING):
    return subprocess.CompletedProcess(["amidi", "-l"], 0, stdout=stdout, stderr="")


def test_discovery_passes_a_timeout_to_amidi():
    seen = {}

    def runner(args, **kwargs):
        seen.update(kwargs)
        return completed()

    assert discover_nanocore_midi_port(runner=runner, timeout=1.5) == "hw:6,0,0"
    assert seen["timeout"] == 1.5


def test_discovery_timeout_is_a_clear_error():
    def runner(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    with pytest.raises(DeviceNotFound, match="timed out"):
        discover_nanocore_midi_port(runner=runner)


def test_discovery_failure_keeps_stderr():
    def runner(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="no soundcards found")

    with pytest.raises(DeviceNotFound, match="no soundcards found"):
        discover_nanocore_midi_port(runner=runner)


def test_discovery_missing_amidi_is_clear():
    def runner(args, **kwargs):
        raise FileNotFoundError(args[0])

    with pytest.raises(RuntimeError, match="amidi is not installed"):
        discover_nanocore_midi_port(runner=runner)


def test_async_discovery_runs_off_the_event_loop():
    threads = []

    def runner(args, **kwargs):
        threads.append(threading.current_thread())
        return completed()

    async def scenario():
        return await discover_nanocore_midi_port_async(runner=runner)

    assert asyncio.run(scenario()) == "hw:6,0,0"
    assert threads[0] is not threading.main_thread()
