#!/usr/bin/env python3
"""Forward a USB MIDI controller (a BOSS GT-10, a footswitch board...) to a NANOCORE.

The pedal's USB port and Bluetooth are both device-only, so something has to sit between a controller
and the pedal and play the host: a Raspberry Pi, a laptop. This script is that middle piece. It reads
the controller's raw MIDI port, translates what it sends and writes the result to the pedal's raw MIDI
port. It uses only the Python standard library and the ALSA raw MIDI nodes (/dev/snd/midiC*D*).

What it translates (see gt10.example.json):

* Program Change is forwarded, on the pedal's channel, up to a maximum (the pedal has 64 presets).
  The controller's bank (Bank Select MSB) decides which Program Changes count: "banks" gives, per
  bank, the number added to the program, so a bank the pedal does not have can be moved onto presets.
* Bank Select (CC 0 and CC 32) is never forwarded. The pedal treats a bank other than 0 as a bank it
  does not have and then ignores every Program Change until it gets bank 0 again.
* A controller can be passed as it is, renumbered ("momentary"), turned from a momentary switch into
  an on/off toggle ("toggle"), or scaled ("map", for an expression pedal).
* Everything else (notes, pitch bend, SysEx, clock, unlisted controllers) is dropped.

The toggle state lives here: the router cannot read the pedal, so after a preset change a toggle can
disagree with the effect it drives. Use "momentary" or "pass" where that matters. A toggle starts in
its "initial" state (a controller whose LED starts lit wants "initial": true, so that the first press
turns the effect off); "reset_toggles_on_program_change" sends every toggle back to its initial state
with each Program Change, for controllers that do the same with their LEDs.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BANK_SELECT_MSB = 0
BANK_SELECT_LSB = 32
ACTIONS = ("pass", "momentary", "toggle", "map")


class Translator:
    """Bytes in, translated messages out. No I/O, so it can be tested without hardware."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.output_channel = _channel(config.get("output_channel", 1), "output_channel")
        self.input_channel = (
            None if config.get("input_channel") is None else _channel(config["input_channel"], "input_channel")
        )
        self.drop_bank_select = bool(config.get("drop_bank_select", True))
        pc = config.get("program_change", {})
        self.program_change = None
        if pc is not None:
            maximum = pc.get("max", 63)
            offset = pc.get("offset", 0)
            if not (isinstance(maximum, int) and 0 <= maximum <= 127):
                raise ValueError("program_change.max must be 0 to 127")
            if not (isinstance(offset, int) and 0 <= offset <= 127):
                raise ValueError("program_change.offset must be 0 to 127")
            banks = pc.get("banks", {"0": offset})
            if not isinstance(banks, dict):
                raise ValueError("program_change.banks must map a bank number to an offset")
            parsed_banks = {}
            for bank, shift in banks.items():
                if not (isinstance(shift, int) and -127 <= shift <= 127):
                    raise ValueError("program_change.banks offsets must be whole numbers from -127 to 127")
                parsed_banks[_data_byte(bank, "bank number")] = shift
            self.program_change = (parsed_banks, maximum)
        self.rules: dict[int, dict[str, Any]] = {}
        self.toggles: dict[int, bool] = {}
        for key, rule in config.get("cc", {}).items():
            number = _data_byte(key, "controller number")
            action = rule.get("action")
            if action not in ACTIONS:
                raise ValueError(f"controller {key}: action must be one of {', '.join(ACTIONS)}")
            send = number if action == "pass" else rule.get("send")
            if send is None:
                raise ValueError(f"controller {key}: {action} needs a 'send' controller number")
            parsed = {"action": action, "send": _data_byte(send, "send")}
            if action == "map":
                parsed["min"] = _data_byte(rule.get("min", 0), "min")
                parsed["max"] = _data_byte(rule.get("max", 127), "max")
            if action == "toggle":
                self.toggles[number] = bool(rule.get("initial", False))
            self.rules[number] = parsed
        self.reset_toggles = bool(config.get("reset_toggles_on_program_change", False))
        self.initial_toggles = dict(self.toggles)
        self.bank = 0  # the last Bank Select MSB seen
        self.last_sent: dict[int, int] = {}
        self._status: int | None = None
        self._data: list[int] = []
        self._in_sysex = False

    # -- parsing -----------------------------------------------------------------------------

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        for byte in data:
            if byte >= 0xF8:  # real-time bytes may appear anywhere, even inside a message
                continue
            if byte & 0x80:
                if byte == 0xF0:
                    self._in_sysex = True
                    self._status, self._data = None, []
                elif byte == 0xF7:
                    self._in_sysex = False
                    self._status, self._data = None, []
                elif byte >= 0xF1:  # other system common messages cancel running status
                    self._status, self._data = None, []
                else:
                    self._in_sysex = False
                    self._status, self._data = byte, []
                continue
            if self._in_sysex or self._status is None:
                continue
            self._data.append(byte)
            if len(self._data) == _data_length(self._status):
                out.extend(self._translate(self._status, self._data))
                self._data = []
        return out

    # -- translation ---------------------------------------------------------------------------

    def _translate(self, status: int, data: list[int]) -> list[bytes]:
        kind, channel = status & 0xF0, status & 0x0F
        if self.input_channel is not None and channel + 1 != self.input_channel:
            return []
        if kind == 0xC0 and self.reset_toggles:
            # a controller that restarts its switches' LEDs with every patch needs its toggles to restart too
            self.toggles = dict(self.initial_toggles)
        if kind == 0xC0 and self.program_change is not None:
            banks, maximum = self.program_change
            if self.bank not in banks:
                return []
            program = data[0] + banks[self.bank]
            return [self._message(0xC0, program)] if 0 <= program <= maximum else []
        if kind != 0xB0:
            return []
        number, value = data
        if number == BANK_SELECT_MSB:
            self.bank = value
        if self.drop_bank_select and number in (BANK_SELECT_MSB, BANK_SELECT_LSB):
            return []
        rule = self.rules.get(number)
        if rule is None:
            return []
        action, send = rule["action"], rule["send"]
        if action in ("pass", "momentary"):
            return [self._message(0xB0, send, value)]
        if action == "toggle":
            if value < 64:  # the release of the switch
                return []
            self.toggles[number] = not self.toggles[number]
            return [self._message(0xB0, send, 127 if self.toggles[number] else 0)]
        low, high = rule["min"], rule["max"]
        scaled = low + round(value * (high - low) / 127)
        if self.last_sent.get(send) == scaled:  # an expression pedal repeats values constantly
            return []
        self.last_sent[send] = scaled
        return [self._message(0xB0, send, scaled)]

    def _message(self, kind: int, *data: int) -> bytes:
        return bytes([kind | (self.output_channel - 1), *data])


def _channel(value: Any, name: str) -> int:
    if not (isinstance(value, int) and 1 <= value <= 16):
        raise ValueError(f"{name} must be 1 to 16")
    return value


def _data_byte(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number from 0 to 127") from None
    if not 0 <= number <= 127:
        raise ValueError(f"{name} must be a number from 0 to 127")
    return number


_ASEQ_CC = re.compile(r"Control change\s+(\d+),\s*controller\s+(\d+),\s*value\s+(\d+)")
_ASEQ_PC = re.compile(r"Program change\s+(\d+),\s*program\s+(\d+)")


def parse_aseqdump_line(line: str) -> bytes | None:
    """One line of ``aseqdump`` as the MIDI message it describes; None for anything else."""

    match = _ASEQ_CC.search(line)
    if match:
        channel, number, value = (int(g) for g in match.groups())
        return bytes([0xB0 | (channel & 0x0F), number & 0x7F, value & 0x7F])
    match = _ASEQ_PC.search(line)
    if match:
        channel, program = (int(g) for g in match.groups())
        return bytes([0xC0 | (channel & 0x0F), program & 0x7F])
    return None


def _data_length(status: int) -> int:
    return 1 if status & 0xF0 in (0xC0, 0xD0) else 2


# -- ALSA raw MIDI ports ---------------------------------------------------------------------------


def find_ports(root: Path = Path("/proc/asound"), dev: Path = Path("/dev/snd")) -> list[tuple[str, Path]]:
    """Every raw MIDI port as (description, node), for example ('GT-10 GT-10 MIDI 1', /dev/snd/midiC2D0)."""

    ports = []
    for card_dir in sorted(root.glob("card[0-9]*")):
        number = card_dir.name[4:]
        try:
            card_id = (card_dir / "id").read_text().strip()
            name = _card_name(root / "cards", number)
        except OSError:
            continue
        for midi in sorted(card_dir.glob("midi[0-9]*")):
            node = dev / f"midiC{number}D{midi.name[4:]}"
            try:
                label = next(line.split(":", 1)[1].strip() for line in midi.read_text().splitlines() if line.startswith("Name:"))
            except (OSError, StopIteration):
                label = ""
            ports.append((f"{card_id} {name} {label}".strip(), node))
    return ports


def _card_name(cards: Path, number: str) -> str:
    lines = cards.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(number + " ["):
            return line.split(" - ", 1)[-1].strip() + " " + (lines[i + 1].strip() if i + 1 < len(lines) else "")
    return ""


def pick_port(ports: list[tuple[str, Path]], match: str) -> Path | None:
    wanted = match.lower()
    found = [node for label, node in ports if wanted in label.lower()]
    return found[0] if found else None


def run(
    config: dict[str, Any], input_match: str, output_match: str, verbose: bool = False, input_mode: str = "seq"
) -> None:
    """Forward until interrupted; wait for the ports to appear and come back after an unplug.

    ``seq`` reads the controller through the ALSA sequencer (``aseqdump``), which other programs can
    share, PiPedal for one. ``raw`` opens its raw MIDI node, which only one program can hold.
    """

    translator = Translator(config)
    while True:
        ports = find_ports()
        target = pick_port(ports, output_match)
        source = pick_port(ports, input_match)  # also tells whether the controller is plugged in
        if source is None or target is None:
            missing = input_match if source is None else output_match
            _log(f"waiting for a MIDI port matching {missing!r}")
            time.sleep(1.0)
            continue
        try:
            if input_mode == "seq":
                _forward_seq(translator, input_match, target, verbose)
            else:
                _forward(translator, source, target, verbose)
        except OSError as exc:
            _log(f"lost a port ({exc}); looking for it again")
            time.sleep(1.0)


def _forward_seq(translator: Translator, seq_name: str, target: Path, verbose: bool) -> None:
    write_fd = os.open(target, os.O_WRONLY)
    process = subprocess.Popen(
        ["aseqdump", "-p", seq_name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
    )
    _log(f"forwarding sequencer port {seq_name!r} -> {target}")
    try:
        assert process.stdout is not None
        for line in process.stdout:
            raw = parse_aseqdump_line(line)
            if raw is None:
                continue
            for message in translator.feed(raw):
                os.write(write_fd, message)
                if verbose:
                    _log("sent " + message.hex(" "))
        raise OSError(errno.ENODEV, "aseqdump ended")
    finally:
        process.kill()
        process.wait()
        os.close(write_fd)


def _forward(translator: Translator, source: Path, target: Path, verbose: bool) -> None:
    read_fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
    write_fd = os.open(target, os.O_WRONLY)
    _log(f"forwarding {source} -> {target}")
    try:
        while True:
            select.select([read_fd], [], [])
            try:
                data = os.read(read_fd, 256)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.EIO):
                    raise
                continue
            if not data:
                raise OSError(errno.ENODEV, "the input port closed")
            for message in translator.feed(data):
                os.write(write_fd, message)
                if verbose:
                    _log("sent " + message.hex(" "))
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forward a MIDI controller to a NANOCORE")
    parser.add_argument("--config", type=Path, help="JSON file with the translation rules")
    parser.add_argument("--input", default="GT-10", help="text found in the controller's port name (default GT-10)")
    parser.add_argument("--output", default="Nanocore", help="text found in the pedal's port name (default Nanocore)")
    parser.add_argument(
        "--input-mode",
        choices=("seq", "raw"),
        default="seq",
        help="seq (default) shares the controller with other programs through ALSA; raw opens its MIDI node",
    )
    parser.add_argument("--list", action="store_true", help="show the raw MIDI ports and exit")
    parser.add_argument("--verbose", action="store_true", help="log every message sent to the pedal")
    args = parser.parse_args(argv)
    if args.list:
        for label, node in find_ports():
            print(f"{node}  {label}")
        return 0
    if args.config is None:
        parser.error("--config is required")
    try:
        config = json.loads(args.config.read_text())
        Translator(config)
    except (OSError, ValueError) as exc:
        print(f"bad configuration: {exc}", file=sys.stderr)
        return 2
    try:
        run(config, args.input, args.output, args.verbose, args.input_mode)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
