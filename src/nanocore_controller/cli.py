"""Command-line interface for the NANOCORE controller."""

import argparse
import asyncio
import dataclasses
import enum
import importlib
import json
import logging
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import assets_protocol as assets
from .asset_backup import backup_assets
from .backup_io import load_backup
from .ble_midi import BleMidiSession, capture_ble
from .ble_restore import build_restore_plan
from .config import (
    DeviceProfile,
    default_config_path,
    detect_adapter,
    load_profile,
    save_profile,
    validate_adapter_name,
    validate_bluetooth_address,
)
from .device import NanocoreDevice
from .discovery import discover_nanocore_midi_port
from .ead_install import decrypt_ead, resolve_decryptor
from .errors import DeviceTimeout, NanocoreError, PartialApplyError, ValidationError
from .ir_import import wav_to_ir
from .mappings import BLOCKS, normalize_block, parameter_cc, percent_to_midi, resolve_type
from .midi import control_change, program_change
from .nanocore_protocol import (
    FIRMWARE_104_SELECT_PRESET_COMMAND,
    SET_SETTINGS_COMMAND,
    encode_request,
    global_settings_payload,
    preset_name_payload,
    select_preset_payload,
    validate_display_number,
)
from .safe_files import write_new
from .usb_midi import AmidiSession

ALIASES = {
    "ble-status": "status",
    "ble-catalog": "presets",
    "ble-backup": "backup",
    "ble-restore": "restore",
}


def _add_restore_arguments(parser: argparse.ArgumentParser, *, legacy: bool = False) -> None:
    parser.add_argument("backup", help="NANOCORE active-preset backup JSON")
    parser.add_argument("--apply", action="store_true", help="apply the validated restore plan")
    if legacy:
        parser.add_argument("--address", dest="legacy_address_override")
        parser.add_argument("--adapter", dest="legacy_adapter")
    parser.add_argument("--safety-backup", help="new mandatory pre-restore backup path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nanocore")
    parser.add_argument("--transport", choices=("bluetooth", "usb"), default="bluetooth")
    parser.add_argument("--address", help="NANOCORE Bluetooth address")
    parser.add_argument("--adapter", help="BlueZ adapter, e.g. hci0 (default: detected)")
    parser.add_argument("--config", help="device profile JSON path")
    parser.add_argument("--channel", type=int, default=1, help="MIDI channel, 1-16 (default: 1)")
    parser.add_argument("--port", help="ALSA MIDI port, e.g. hw:6,0,0")
    parser.add_argument("--dry-run", action="store_true", help="print bytes without connecting")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log to stderr: -v audit trail of writes, -vv everything",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("devices", help="show the configured Bluetooth or discovered USB endpoint")
    configure = commands.add_parser("configure", help="save the default Bluetooth device")
    configure.add_argument("profile_address", metavar="ADDRESS")
    configure.add_argument("--adapter", dest="command_adapter")

    commands.add_parser("presets", help="read all preset numbers and names")
    commands.add_parser("status", help="read the complete active preset")
    preset = commands.add_parser("preset", help="recall a visible preset number (1-128)")
    preset.add_argument("number", type=int)
    backup = commands.add_parser("backup", help="back up the active preset to a new JSON file")
    backup.add_argument("output")
    restore = commands.add_parser("restore", help="print a restore plan; --apply performs it")
    _add_restore_arguments(restore)
    save = commands.add_parser("save", help="save the active preset permanently")
    save.add_argument("--safety-backup", help="new backup path written before saving")

    settings = commands.add_parser(
        "settings", help="show the pedal-wide settings; with options, print a plan, and --apply changes them"
    )
    settings.add_argument("--bluetooth", choices=("on", "off"), help="the pedal's own Bluetooth")
    settings.add_argument("--loopback", choices=("on", "off"))
    settings.add_argument("--input-gain", type=int, help="input gain in dB, -20 to 20")
    settings.add_argument("--usb-volume", type=int, help="0 to 100")
    settings.add_argument("--bt-volume", type=int, help="0 to 100")
    settings.add_argument("--midi-channel", type=int, help="0 (omni) to 16")
    settings.add_argument("--apply", action="store_true", help="write the changes to the pedal")
    assets_cmd = commands.add_parser("assets", help="list, read or back up the amplifier and IR slots (read only)")
    assets_sub = assets_cmd.add_subparsers(dest="assets_command", required=True)
    assets_sub.add_parser("list", help="every slot of both storages")
    assets_read = assets_sub.add_parser("read", help="read one slot into a new file")
    assets_read.add_argument("kind", choices=("amp", "ir"))
    assets_read.add_argument("slot", type=int)
    assets_read.add_argument("output")
    assets_write = assets_sub.add_parser(
        "write", help="replace one slot with the content of a file; prints a plan unless --apply"
    )
    assets_write.add_argument("kind", choices=("amp", "ir"))
    assets_write.add_argument("slot", type=int)
    assets_write.add_argument("file")
    assets_write.add_argument("--name", help="new slot name, up to 15 printable ASCII characters")
    assets_write.add_argument("--safety-backup", help="new file that receives the content the slot has now")
    assets_write.add_argument("--decryptor", help="program that decrypts an .ead file (see docs/ead-decryptor.md)")
    assets_write.add_argument("--apply", action="store_true", help="write to the pedal")
    assets_backup_cmd = assets_sub.add_parser("backup", help="read every slot into a new directory")
    assets_backup_cmd.add_argument("directory")
    rename = commands.add_parser("rename", help="rename the active preset (up to 8 characters) and store it")
    rename.add_argument("name")
    rename.add_argument("--safety-backup", help="new backup path written before saving")
    commands.add_parser("next", help="select the next preset")
    commands.add_parser("previous", help="select the previous preset")
    tuner = commands.add_parser("tuner", help="control the tuner")
    tuner.add_argument("state", choices=("on", "off"))
    block = commands.add_parser("block", help="turn an effect block on or off")
    block.add_argument("name")
    block.add_argument("state", choices=("on", "off"))
    effect_type = commands.add_parser("type", help="select an effect type")
    effect_type.add_argument("block")
    effect_type.add_argument("type_name")
    parameter = commands.add_parser("param", help="set a documented parameter")
    parameter.add_argument("name")
    parameter.add_argument("value", help="0-127 or a percentage such as 50%")

    capture = commands.add_parser("ble-capture", help="capture BLE-MIDI notifications")
    capture.add_argument("capture_address")
    capture.add_argument("--seconds", type=float, default=10.0)
    capture.add_argument("--output")
    capture.add_argument("--adapter", dest="command_adapter")
    capture.add_argument("--backend", choices=("auto", "bleak", "gatttool"), default="auto")

    commands.add_parser(
        "serve",
        add_help=False,
        help="run the local web server (everything after 'serve' is passed on)",
    )

    for name, help_text in (
        ("ble-status", "legacy alias for status"),
        ("ble-catalog", "legacy alias for presets"),
    ):
        alias = commands.add_parser(name, help=help_text)
        alias.add_argument("legacy_address")
        alias.add_argument("--adapter", dest="legacy_adapter")
    legacy_backup = commands.add_parser("ble-backup", help="legacy alias for backup")
    legacy_backup.add_argument("legacy_address")
    legacy_backup.add_argument("output")
    legacy_backup.add_argument("--adapter", dest="legacy_adapter")
    legacy_restore = commands.add_parser("ble-restore", help="legacy alias for restore")
    _add_restore_arguments(legacy_restore, legacy=True)
    return parser


def parse_value(raw: str) -> int:
    value = raw.strip()
    if value.endswith("%"):
        try:
            return percent_to_midi(float(value[:-1]))
        except ValueError as exc:
            raise ValueError(f"invalid percentage: {raw}") from exc
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"value must be 0-127 or a percentage: {raw}") from exc
    if not 0 <= result <= 127:
        raise ValueError("value must be from 0 to 127")
    return result


def command_payload(args: argparse.Namespace) -> bytes | None:
    command = ALIASES.get(args.command, args.command)
    if COMMANDS[command].dry_run is not DryRun.BYTES:
        return None
    if command == "preset":
        return program_change(args.channel, validate_display_number(args.number) - 1)
    if command in ("next", "previous"):
        return control_change(args.channel, 82 if command == "next" else 81, 127)
    if command == "tuner":
        return control_change(args.channel, 80, 127 if args.state == "on" else 0)
    if command == "block":
        block = BLOCKS[normalize_block(args.name)]
        return control_change(args.channel, block.on_cc, 127 if args.state == "on" else 0)
    if command == "type":
        block_name = normalize_block(args.block)
        block = BLOCKS[block_name]
        return control_change(args.channel, block.type_cc, resolve_type(block_name, args.type_name))
    if command == "param":
        return control_change(args.channel, parameter_cc(args.name), parse_value(args.value))
    raise ValueError(f"unknown command: {args.command}")




def _print_json(value: object, output: TextIO) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False), file=output)


def _print_plan(document: Mapping[str, Any], output: TextIO) -> None:
    _print_json(
        [
            {
                "description": operation.description,
                "command": f"0x{operation.command:02x}",
                "payload": operation.payload.hex(" "),
            }
            for operation in build_restore_plan(document)
        ],
        output,
    )


def _resolve_endpoint(
    args: argparse.Namespace, backup: Mapping[str, Any] | None = None
) -> DeviceProfile:
    explicit_address = (
        getattr(args, "legacy_address_override", None)
        or getattr(args, "legacy_address", None)
        or args.address
    )
    explicit_adapter = getattr(args, "legacy_adapter", None) or args.adapter
    profile = None
    if explicit_address is None or explicit_adapter is None:
        try:
            profile = load_profile(args.config)
        except FileNotFoundError:
            pass
    metadata = backup.get("device", {}) if backup is not None else {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    address = explicit_address or (profile.address if profile else None) or metadata.get("address")
    adapter = (
        explicit_adapter
        or (profile.adapter if profile else None)
        or metadata.get("adapter")
        or detect_adapter()
    )
    if not address:
        path = Path(args.config) if args.config else default_config_path()
        raise ValidationError(
            f"no Bluetooth device configured; run 'nanocore --config {path} configure ADDRESS'"
        )
    return DeviceProfile(address, adapter)


def create_device(
    args: argparse.Namespace, document: Mapping[str, Any] | None = None
) -> NanocoreDevice:
    """Build the device for the chosen transport; nothing is opened until it is entered."""

    if args.transport == "usb":
        port = args.port or discover_nanocore_midi_port()
        return NanocoreDevice(AmidiSession(port), device_info={"transport": "usb", "port": port})
    profile = _resolve_endpoint(args, document)
    return NanocoreDevice(
        BleMidiSession(profile.address, adapter=profile.adapter),
        device_info={"address": profile.address, "adapter": profile.adapter},
    )


DeviceFactory = Callable[[argparse.Namespace, Mapping[str, Any] | None], NanocoreDevice]
DeviceWork = Callable[[NanocoreDevice], Awaitable[None]]


@dataclass
class Context:
    args: argparse.Namespace
    output: TextIO
    device_factory: DeviceFactory

    def run_on_device(
        self, work: DeviceWork, document: Mapping[str, Any] | None = None
    ) -> None:
        device = self.device_factory(self.args, document)

        async def session() -> None:
            async with device:
                await work(device)

        asyncio.run(session())


Handler = Callable[[Context], int | None]


class DryRun(enum.Enum):
    IGNORED = "runs as usual"
    REFUSED = "refused"
    BYTES = "prints the MIDI bytes"


BLUETOOTH = frozenset({"bluetooth"})
ANY_TRANSPORT = frozenset({"bluetooth", "usb"})


@dataclass(frozen=True)
class Command:
    handler: Handler
    transports: frozenset[str] = ANY_TRANSPORT
    dry_run: DryRun = DryRun.REFUSED


def _configure(ctx: Context) -> None:
    args = ctx.args
    path = Path(args.config) if args.config else default_config_path()
    profile = DeviceProfile(args.profile_address, args.command_adapter or detect_adapter())
    save_profile(profile, path)
    print(f"Bluetooth device saved to {path}", file=ctx.output)


def _devices(ctx: Context) -> None:
    args = ctx.args
    if args.transport == "usb":
        if args.dry_run and not args.port:
            raise ValidationError("--dry-run devices with USB requires --port")
        port = args.port or discover_nanocore_midi_port()
        print(f"Nanocore USB MIDI: {port}", file=ctx.output)
    else:
        profile = _resolve_endpoint(args)
        print(f"Nanocore Bluetooth: {profile.address} via {profile.adapter}", file=ctx.output)


def _ble_capture(ctx: Context) -> None:
    args = ctx.args
    captured = asyncio.run(
        capture_ble(
            validate_bluetooth_address(args.capture_address),
            seconds=args.seconds,
            output_path=args.output,
            handshake=False,
            backend=args.backend,
            adapter=validate_adapter_name(args.command_adapter or detect_adapter()),
        )
    )
    if args.output:
        print(
            f"captured {len(captured['raw_packets'])} BLE-MIDI packet(s) to {args.output}",
            file=ctx.output,
        )
    else:
        print(json.dumps(captured, indent=2), file=ctx.output)


def _status(ctx: Context) -> None:
    async def work(device: NanocoreDevice) -> None:
        _print_json(await device.read_state(), ctx.output)

    ctx.run_on_device(work)


def _presets(ctx: Context) -> None:
    async def work(device: NanocoreDevice) -> None:
        _print_json(await device.catalog(), ctx.output)

    ctx.run_on_device(work)


def _backup(ctx: Context) -> None:
    path = Path(ctx.args.output)

    async def work(device: NanocoreDevice) -> None:
        await device.backup_active(path)
        print(f"backup written to {path}", file=ctx.output)

    ctx.run_on_device(work)


def _save(ctx: Context) -> None:
    if not ctx.args.safety_backup:
        raise ValidationError("--safety-backup is required for save")
    safety_path = Path(ctx.args.safety_backup)

    async def work(device: NanocoreDevice) -> None:
        snapshot = await device.save_active(safety_path)
        print(
            f"saved preset {snapshot.active_preset + 1} permanently; "
            f"safety backup written to {safety_path}",
            file=ctx.output,
        )

    ctx.run_on_device(work)


def _settings(ctx: Context) -> None:
    args = ctx.args
    changes: dict[str, Any] = {
        "wireless_enabled": None if args.bluetooth is None else args.bluetooth == "on",
        "loopback_enabled": None if args.loopback is None else args.loopback == "on",
        "input_gain_db": args.input_gain,
        "usb_volume": args.usb_volume,
        "bt_volume": args.bt_volume,
        "midi_channel": args.midi_channel,
    }
    given = {name: value for name, value in changes.items() if value is not None}
    if not given:
        if args.apply:
            raise ValidationError("--apply needs at least one setting to change")

        async def show(device: NanocoreDevice) -> None:
            _print_json(dataclasses.asdict(await device.read_global_settings()), ctx.output)

        ctx.run_on_device(show)
        return
    payload = global_settings_payload(**given)  # validates before anything is opened
    if not args.apply:
        print(
            f"plan: command 0x{SET_SETTINGS_COMMAND:02x} with payload {payload.hex(' ')}"
            f" ({', '.join(f'{k}={v}' for k, v in given.items())})",
            file=ctx.output,
        )
        print("nothing was sent; add --apply to change the pedal", file=ctx.output)
        return
    if args.dry_run:
        raise ValidationError("--dry-run cannot be combined with --apply")

    async def work(device: NanocoreDevice) -> None:
        settings = await device.write_global_settings(**given)
        _print_json(dataclasses.asdict(settings), ctx.output)

    ctx.run_on_device(work)


def _assets(ctx: Context) -> None:
    args = ctx.args
    kinds = {"amp": assets.AMP, "ir": assets.IR}
    data = b""
    if args.assets_command == "write":
        data = Path(args.file).read_bytes()
        converted = False
        decrypted = False
        if args.kind == "amp" and data[:4] in (b"SAPF", b"EADL"):
            data = decrypt_ead(data, resolve_decryptor(args.decryptor))  # the user's own decryptor
            decrypted = True
        if args.kind == "ir" and data[:4] == b"RIFF":
            data = wav_to_ir(data)  # a WAV is converted to the pedal's IR format
            converted = True
        assets.slot_info_payload(args.slot)
        if args.kind == "ir":
            assets.validate_ir(data)
        else:
            assets.validate_stored_amp(data)
        steps = assets.write_sequence(kinds[args.kind], args.slot, data, name=args.name)  # validates
        if not args.apply:
            print(
                f"plan: write {len(data)} bytes (crc32 {assets.crc32(data):08x}) to {args.kind} slot {args.slot}"
                f" in {len(steps)} steps" + (f", name {args.name!r}" if args.name else "")
                + (", converted from WAV (mono, 48 kHz, 1024 samples at most, peak 1.0)" if converted else "")
                + (", decrypted by your decryptor" if decrypted else ""),
                file=ctx.output,
            )
            print("nothing was sent; add --apply and --safety-backup PATH to change the pedal", file=ctx.output)
            return
        if not args.safety_backup:
            raise ValidationError("--safety-backup is required with --apply")
        if args.dry_run:
            raise ValidationError("--dry-run cannot be combined with --apply")

    async def work(device: NanocoreDevice) -> None:
        if args.assets_command == "list":
            listing = {
                name: [
                    {"slot": a.slot, "name": a.name, "size": a.size, "crc32": a.checksum, "active": a.active}
                    for a in await device.list_assets(kind)
                ]
                for name, kind in kinds.items()
            }
            _print_json(listing, ctx.output)
        elif args.assets_command == "read":
            info, contents = await device.read_asset(kinds[args.kind], args.slot)
            write_new(Path(args.output), contents)
            print(
                f"{args.kind} slot {info.slot} ({info.name}): {len(contents)} bytes, crc32 {assets.crc32(contents):08x} "
                f"verified; written to {args.output}",
                file=ctx.output,
            )
        elif args.assets_command == "write":
            written = await device.write_asset(
                kinds[args.kind], args.slot, data, safety_backup=Path(args.safety_backup), name=args.name
            )
            print(
                f"{args.kind} slot {written.slot} now holds {written.name!r}: {written.size} bytes, crc32 "
                f"{written.checksum:08x}, read back and verified; its previous content is in {args.safety_backup}",
                file=ctx.output,
            )
        else:
            manifest = await backup_assets(device, Path(args.directory), progress=lambda line: print(line, file=ctx.output))
            print(f"{len(manifest['items'])} slots written to {args.directory}", file=ctx.output)

    ctx.run_on_device(work)


def _rename(ctx: Context) -> None:
    preset_name_payload(ctx.args.name)  # validates before anything is opened
    if not ctx.args.safety_backup:
        raise ValidationError("--safety-backup is required for rename")
    if ctx.args.dry_run:
        raise ValidationError("--dry-run cannot be combined with rename")
    safety_path = Path(ctx.args.safety_backup)

    async def work(device: NanocoreDevice) -> None:
        name = await device.rename_active(ctx.args.name, safety_path)
        print(f"renamed and stored as {name!r}; safety backup written to {safety_path}", file=ctx.output)

    ctx.run_on_device(work)


def _restore(ctx: Context) -> None:
    args = ctx.args
    document = load_backup(Path(args.backup))
    if not args.apply:
        _print_plan(document, ctx.output)
        return
    if args.dry_run:
        raise ValidationError("--dry-run cannot be combined with --apply")
    if not args.safety_backup:
        raise ValidationError("--safety-backup is required with --apply")
    build_restore_plan(document)
    safety_path = Path(args.safety_backup)

    async def work(device: NanocoreDevice) -> None:
        result = await device.restore_active(document, safety_path)
        print(
            f"restored {result['operations_applied']} operations; read-back verified; "
            f"previous state saved to {safety_path}",
            file=ctx.output,
        )

    ctx.run_on_device(work, document)


def _preset(ctx: Context) -> None:
    number = validate_display_number(ctx.args.number)
    if ctx.args.transport == "usb":
        _send(ctx, read_back=False)
        return

    async def work(device: NanocoreDevice) -> None:
        await device.select_preset(number)
        print(f"selected preset {number} over Bluetooth", file=ctx.output)

    ctx.run_on_device(work)


def _send(ctx: Context, *, read_back: bool) -> None:
    payload = command_payload(ctx.args)
    assert payload is not None

    async def work(device: NanocoreDevice) -> None:
        await device.send_midi(payload)
        print(f"sent bytes: {payload.hex(' ')}", file=ctx.output)
        if read_back:
            await device.read_live()

    ctx.run_on_device(work)


def _send_midi(ctx: Context) -> None:
    _send(ctx, read_back=False)


def _send_midi_verified(ctx: Context) -> None:
    _send(ctx, read_back=True)


SERVE_NEEDS_EXTRA = (
    "nanocore serve needs the 'serve' extra: pip install 'nanocore-controller[serve]'"
)


def _serve(ctx: Context) -> int:
    try:
        server = importlib.import_module(f"{__package__}.serve")
    except ImportError as exc:
        raise RuntimeError(SERVE_NEEDS_EXTRA) from exc
    args = ctx.args
    if args.config:
        raise ValidationError("--config does not apply to serve")
    # Connection options given before the word serve become the server's own; the ones
    # after it come later on the line and win.
    forwarded: list[str] = []
    if args.transport != "bluetooth":
        forwarded += ["--transport", args.transport]
    if args.address:
        forwarded += ["--address", args.address]
    if args.adapter:
        forwarded += ["--adapter", args.adapter]
    if args.port:
        forwarded += ["--alsa-port", args.port]
    return int(server.main(forwarded + list(args.serve_args)))


COMMANDS: dict[str, Command] = {
    "configure": Command(_configure, dry_run=DryRun.IGNORED),
    "devices": Command(_devices, dry_run=DryRun.IGNORED),
    "ble-capture": Command(_ble_capture, BLUETOOTH),
    "serve": Command(_serve),
    "status": Command(_status),
    "presets": Command(_presets),
    "backup": Command(_backup),
    "save": Command(_save),
    "restore": Command(_restore, dry_run=DryRun.IGNORED),
    "settings": Command(_settings, dry_run=DryRun.IGNORED),
    "rename": Command(_rename, dry_run=DryRun.IGNORED),
    "assets": Command(_assets, dry_run=DryRun.IGNORED),
    "preset": Command(_preset, dry_run=DryRun.BYTES),
    "next": Command(_send_midi, dry_run=DryRun.BYTES),
    "previous": Command(_send_midi, dry_run=DryRun.BYTES),
    "tuner": Command(_send_midi, dry_run=DryRun.BYTES),
    "block": Command(_send_midi_verified, dry_run=DryRun.BYTES),
    "type": Command(_send_midi_verified, dry_run=DryRun.BYTES),
    "param": Command(_send_midi_verified, dry_run=DryRun.BYTES),
}


def _dry_run_payload(args: argparse.Namespace, command: str) -> bytes:
    if command == "preset" and args.transport == "bluetooth":
        return encode_request(
            FIRMWARE_104_SELECT_PRESET_COMMAND,
            select_preset_payload(validate_display_number(args.number) - 1),
            sequence=0,
        )
    payload = command_payload(args)
    assert payload is not None
    return payload


_CLI_HANDLER = "nanocore-cli-handler"


def configure_logging(verbosity: int) -> None:
    """Send the library's log records to stderr; only the CLI ever does this."""

    logger = logging.getLogger("nanocore")
    for handler in list(logger.handlers):
        if handler.get_name() == _CLI_HANDLER:
            logger.removeHandler(handler)
    audit = logging.getLogger("nanocore.audit")
    logger.setLevel(logging.NOTSET)
    audit.setLevel(logging.NOTSET)
    if verbosity <= 0:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_CLI_HANDLER)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    if verbosity == 1:
        audit.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.DEBUG)


def _report_error(exc: Exception, output: TextIO) -> None:
    print(f"error: {exc}", file=output)
    if isinstance(exc, DeviceTimeout) and exc.maybe_applied:
        print(
            "the command may have been applied; check the current state with 'nanocore status'",
            file=output,
        )
    if isinstance(exc, PartialApplyError):
        for note in getattr(exc, "__notes__", ()):
            print(note, file=output)
        print(f"{exc.applied} of {exc.total} operations were applied", file=output)
        if exc.backup_path is not None:
            print(f"the previous state is saved in {exc.backup_path}", file=output)


EXPECTED_ERRORS = (
    NanocoreError,
    ValueError,
    RuntimeError,
    OSError,
    subprocess.CalledProcessError,
    TimeoutError,
)


# Global options that consume the next argument; needed to find the real subcommand.
_VALUE_OPTIONS = frozenset(
    {"--transport", "--address", "--adapter", "--config", "--channel", "--port"}
)


def _split_serve(arguments: list[str]) -> tuple[list[str], list[str]]:
    """Cut the command line after a ``serve`` subcommand, which parses its own options.

    ``serve`` only counts when it is the first positional argument, so the word
    as an option value or a file name is left alone.
    """

    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "serve":
            return arguments[: index + 1], arguments[index + 1 :]
        if token in _VALUE_OPTIONS:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            break
    return arguments, []


def run(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
    device_factory: DeviceFactory | None = None,
) -> int:
    output = output or sys.stdout
    parser = build_parser()
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        arguments, forwarded = _split_serve(arguments)
        args = parser.parse_args(arguments)
        args.serve_args = forwarded
        configure_logging(args.verbose)
        command = ALIASES.get(args.command, args.command)
        spec = COMMANDS[command]

        if args.dry_run and spec.dry_run is DryRun.REFUSED:
            raise ValidationError(f"--dry-run is not supported for {command}")
        if args.transport not in spec.transports:
            raise ValidationError(f"{command} is not supported with USB transport")
        if args.dry_run and spec.dry_run is DryRun.BYTES:
            print(f"dry-run bytes: {_dry_run_payload(args, command).hex(' ')}", file=output)
            return 0
        result = spec.handler(Context(args, output, device_factory or create_device))
        return 0 if result is None else result
    except EXPECTED_ERRORS as exc:
        _report_error(exc, output)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)
