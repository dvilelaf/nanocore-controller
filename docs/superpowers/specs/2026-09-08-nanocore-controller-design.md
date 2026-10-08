# LIVTRA NANOCORE command controller

## Goal

Create a small local command-line controller for the connected LIVTRA NANOCORE. It must support readable commands for presets and effect parameters, discover the device without hard-coding transient ALSA card numbers, and provide a path to read and back up the actual device state before changing it.

The tool must never silently save, overwrite, restore, or update firmware.

## Current device facts

- USB identity observed on this PC: vendor `33c3`, product `1301`, product name `Nanocore`, manufacturer `Ember`.
- USB exposes both `Nanocore MIDI 1` and `Nanocore USB Audio`.
- The official MIDI guide documents standard MIDI CC and Program Change control for firmware 1.04+.
- The official manual describes the `Nanocore Midi (Data BT)` channel as bidirectional app data synchronization.
- BLE data advertises as `Nanocore Midi` and exposes the BLE-MIDI service `03b80e5a-ede8-4b33-a751-6ce34ec4c700` with data characteristic `7772e5db-3868-4112-a1a9-f2669d106bf3`.
- USB MIDI writes have been verified with tuner control and next-preset control. USB did not emit events when the device was changed physically during an initial observation window.
- A direct BLE connection can discover the service, but the current Linux tools disconnect before notifications can be consumed. The controller therefore needs a dedicated BLE-MIDI session/handshake investigation before claiming live readback.

## User-facing commands

The initial CLI will expose these groups:

```text
nanocore devices
nanocore current
nanocore preset <0-127>
nanocore next|previous
nanocore tuner on|off
nanocore block <fx1|fx2|amp|cab|mod|delay|reverb|eq> on|off
nanocore type <block> <id-or-name>
nanocore param <name> <0-127-or-percent>
nanocore snapshot <file>
nanocore backup <directory>
nanocore restore <backup> --confirm
```

`--dry-run` will print the MIDI transport, channel, status byte, controller, and value without sending anything. Device discovery may be overridden with `--port` and `--ble-address` for diagnostics.

The default channel is MIDI channel 1 (status-byte channel 0), which works with the device's documented channel-0 omni mode. A configurable channel will be available for users who change the device MIDI channel.

## Architecture

### Transport layer

Provide a small transport interface with two implementations:

1. USB/ALSA-MIDI transport, initially using the system `amidi` executable because it is already available and avoids a permanent Python MIDI dependency.
2. BLE-MIDI transport, using the standard BLE-MIDI service and characteristic discovered above. This transport will support notifications and writes once the connection/handshake behavior is understood.

The MIDI encoder will be independent of either transport. This makes all byte-generation behavior testable without hardware.

### Command and state layer

Keep protocol mappings in a declarative table. Include the documented CC map, effect type IDs, direct parameters, preset Program Change, value validation, and aliases such as `reverb mix`.

Separate three kinds of state:

- `observed`: values read from the NANOCORE or received in BLE notifications.
- `sent`: values successfully transmitted by this tool.
- `assumed`: values inferred from a command when no readback exists.

The CLI must label assumed state clearly and must not present it as a device readback.

### Readback and synchronization

The first investigation target is the BLE-MIDI connect sequence used by the iOS app. The implementation should:

1. connect to `Nanocore Midi`;
2. subscribe to the MIDI characteristic notifications;
3. identify any initial SysEx, Program Change, CC, or proprietary message sequence;
4. decode state snapshots and incremental updates;
5. verify the decoder against changes made in the iOS app and by USB MIDI.

If the device requires an app-specific handshake, isolate that handshake in the BLE transport and document the observed message format. Do not send unknown writes during discovery except controlled, reversible MIDI changes.

## Backups and restore safety

The preferred backup is a device-originated export/readback containing all preset slots and their complete parameters, including amp/cab/IR references. Each backup will include:

- timestamp;
- device identity and firmware version when available;
- transport and readback status;
- raw messages, where useful for future decoder improvements;
- normalized preset data;
- SHA-256 checksum of the backup payload.

Before any restore or multi-message preset update, create a backup automatically. Restore will be dry-run by default, showing affected slots and a diff. Actual restore requires `--confirm`. Saving to device memory remains a separate explicit command and is not part of the initial write path.

If direct readback cannot yet be implemented, the tool will refuse to call a partial snapshot a full backup. It may offer a clearly labeled `sent-state snapshot`, while the user can use ToneCommand's official Export Presets workflow as the full-backup fallback.

## Error handling

- Fail before sending if the port is missing, the value is outside 0-127, or a type/parameter is incompatible with the selected block.
- Report the exact transport, MIDI channel, and bytes sent.
- Require an explicit confirmation for restore and any operation that could alter more than one preset.
- Never perform firmware operations.
- On a failed write or failed readback, report the partial operation and do not claim the device matches the requested state.

## Testing strategy

Use test-first development for the controller code:

1. unit tests for MIDI status-byte and CC/PC encoding;
2. unit tests for value normalization, aliases, validation, and effect compatibility;
3. snapshot/backup serialization and checksum tests;
4. BLE-MIDI packet framing/decoding tests using captured fixtures, with no live device required;
5. CLI `--dry-run` tests using an injected fake transport;
6. hardware smoke tests that start with tuner and preset navigation, then a single reversible parameter change;
7. readback verification tests comparing a sent change with the observed notification/state.

No test will save a preset or update firmware automatically.

## Phased delivery

1. Build and test the pure MIDI encoder and CLI dry-run mode.
2. Add robust USB/ALSA discovery and live write commands.
3. Implement BLE-MIDI connection, notification capture, and protocol discovery fixtures.
4. Add live `current` state and verified readback.
5. Add full backups, diffs, and guarded restore.
6. Add name maps for amplifier/cabinet IDs after confirming their exact MIDI indexing.
