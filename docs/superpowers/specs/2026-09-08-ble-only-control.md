# Bluetooth-Only Control Design

## Goal

Make Bluetooth the normal, self-sufficient control path for the LIVTRA NANOCORE. A user must be able to list presets, inspect state, select a preset, back it up, restore it, and change the supported live controls without also connecting USB.

## User interface

- `nanocore configure ADDRESS [--adapter ADAPTER]` stores the default pedal in the user's configuration.
- `nanocore presets`, `status`, `preset NUMBER`, `backup FILE`, and `restore FILE` use that configured Bluetooth device by default.
- Existing live commands (`next`, `previous`, `tuner`, `block`, `type`, and `param`) also use Bluetooth by default.
- `--address` and `--adapter` override saved Bluetooth configuration for one invocation.
- `--transport usb` selects the existing ALSA MIDI path explicitly. USB is optional and is never opened as a fallback from a failed Bluetooth operation.
- The existing `ble-*` commands remain as compatibility aliases during this version.
- Preset numbers shown to and accepted from users are display numbers 1 through 128. Internally they map to slots 0 through 127.

## Architecture

The CLI resolves one connection profile before executing a command. Bluetooth commands use `BleMidiSession`; USB commands use `AmidiTransport`. No command creates both transports.

Bluetooth preset selection uses the private command sent by the official ToneCommand app, reconstructed byte-for-byte from the distributed native library. Other already-supported live controls are sent as MIDI Program Change or Control Change messages inside BLE-MIDI packets. Read, backup, and restore continue to use the private request/response SysEx protocol.

Device configuration lives in an XDG-compatible JSON file (`$XDG_CONFIG_HOME/nanocore-controller/config.json`, otherwise `~/.config/nanocore-controller/config.json`). Configuration loading is strict enough to reject malformed addresses, adapters, or unknown schema versions, and writes are atomic.

## Verification and safety

- Every Bluetooth write opens only Bluetooth, sends the operation, and reads the relevant state back over the same session when the protocol exposes that state.
- Preset recall is verified by reading command `0x63` and checking the active slot.
- Live preset edits remain volatile. No permanent preset-save command is added.
- Backup files keep exclusive-create semantics and are never overwritten.
- Restore still validates its embedded raw snapshot, requires a safety backup before applying, and verifies runtime/amp/IR state afterward.
- `--dry-run` opens no device and prints the bytes that the selected transport would send.
- Bluetooth failure returns an error; it never silently falls back to USB.

## Compatibility and limits

- USB remains available for supported MIDI commands and uses the same display-number convention for preset selection.
- The controller only exposes effect types and parameters already documented in its mappings. Unknown model-specific controls are not guessed.
- Firmware updates and permanent preset storage remain out of scope.

## Acceptance criteria

1. With USB disconnected, a configured pedal can run `presets`, `status`, `preset 9`, `backup`, and restore operations over Bluetooth.
2. With USB disconnected, supported live control commands send valid BLE-MIDI and do not instantiate `AmidiTransport`.
3. `preset 9` selects internal slot 8 and confirms slot 8 by BLE readback.
4. `--transport usb` never initializes Bluetooth; the default Bluetooth path never discovers or opens USB.
5. Existing backup/restore safety properties and compatibility aliases remain covered by tests.
6. Automated tests verify protocol bytes, configuration behavior, transport isolation, CLI routing, and failed readback behavior.
