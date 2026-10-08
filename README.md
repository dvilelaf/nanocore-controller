# NANOCORE Controller

Local, safety-first control for the LIVTRA NANOCORE: a command-line tool, a Python library and a local web editor.

This is an unofficial, independent project. It is not made, endorsed or supported by Livtra. It was written for the author's own pedal; it writes to the pedal's flash, so read the safety notes below and keep a backup. The protocol notes in `docs/` come from observing the pedal and the official app. The project contains no keys and no decryption of Livtra's encrypted `.ead` files; see `docs/ead-decryptor.md`.

- **Command line** (`nanocore`): list and recall presets, read the complete state, send documented live controls, make a lossless backup, restore it, and store the live state permanently, over Bluetooth (default) or USB MIDI. Every command that writes requires a backup path first and verifies by reading back.
- **Library**: `NanocoreDevice` is one transport-independent object over a Bluetooth or USB session, with typed errors, one lock per device, writes that finish even if the caller is cancelled, and an audit log of every write.
- **Web editor** (`nanocore serve`): the whole effect chain on one screen. Every change is applied to the pedal as you make it; it stays in the pedal's working memory until you press the save button next to the preset dropdown. The preset as it was when the server first saw it is kept as a *baseline* (the newest 20 per preset), so an unwanted save can be undone, and every save also keeps a copy of the state it wrote. See [Local web server](#local-web-server).
- **USB audio**: the NANOCORE is also a driver-free USB sound card. [A small service](#usb-audio-through-the-pc-speakers) plays the guitar through the PC speakers with low latency.

Firmware updates are not implemented. The private protocol was reverse engineered from the official ToneCommand app; see [`docs/protocol.md`](docs/protocol.md). The project is independent of Livtra.

## Install

From a checkout (Python 3.11 or newer, [uv](https://docs.astral.sh/uv/)):

```bash
uv sync --extra serve --extra dev      # a virtual environment with everything
.venv/bin/nanocore --help
```

or into the system Python:

```bash
uv pip install --system -e '.[serve]'
```

Bluetooth uses `bleak`, which is a normal dependency. USB MIDI needs read and write access to the ALSA raw MIDI device (see [USB](#usb)). The `serve` extra adds `aiohttp` for the web server. The web editor is built with Node 22 (`web/.nvmrc`): `./scripts/build_web.sh` builds it and copies it into the package.

## Configure once

```bash
bluetoothctl devices                                # find "Nanocore Midi" (pair it once; see below)
nanocore configure AA:BB:CC:DD:EE:FF                # --adapter hci0 is detected when omitted
nanocore devices
```

The profile is stored in `$XDG_CONFIG_HOME/nanocore-controller/config.json`, or `~/.config/nanocore-controller/config.json` when `XDG_CONFIG_HOME` is unset. `--address` and `--adapter` override it for one invocation. Without `--adapter` the lowest-numbered BlueZ adapter is used.

The control endpoint is called `Nanocore Midi`. The pedal hides it until it has been paired once with the official phone app, and it accepts one Bluetooth connection at a time, so close the phone app (or switch the phone's Bluetooth off) before using the PC. The speaker endpoint `Nanocore Audio` is a different thing and is not needed here.

## Bluetooth examples

```bash
nanocore presets
nanocore status
nanocore preset 9
nanocore next
nanocore tuner on
nanocore block reverb on
nanocore type reverb shimmer
nanocore type amp 4
nanocore type cab 5
nanocore param amp_gain 70%
nanocore save --safety-backup backups/before-save.json
nanocore --dry-run param amp_gain 70%
nanocore backup backups/my-preset.json
nanocore restore backups/my-preset.json
nanocore restore backups/my-preset.json --apply --safety-backup backups/before-restore.json
nanocore ble-capture AA:BB:CC:DD:EE:FF --seconds 15 --output /tmp/nanocore-capture.json
```

Preset numbers are visible numbers 1 through 128: `preset 9` recalls internal slot 8. All values are validated before sending. `--dry-run` never opens a device and prints the bytes that would be sent.

`save` writes the current live state permanently into the active preset. It requires a new safety-backup path before sending command `0x46`; the backup can be restored and saved again if the permanent edit needs to be undone.

`presets` reads every preset number and name without recalling them. `status` reads the active preset number and name, volume, all eight effect states and parameters, chain order, and active amplifier and cabinet/IR slots. `backup` writes that same information plus the exact raw snapshot to a new JSON file and refuses to overwrite an existing backup.

`restore BACKUP` is non-writing by default: it validates the JSON against its embedded raw snapshot and asset metadata, then prints every byte that would be sent. Adding `--apply` requires a new `--safety-backup` path and the same preset slot to be active, captures current state before the first write, applies amplifier, IR, variants, exact float parameters, enabled states, volume and chain order, then reads everything back for verification. The operation changes only live state; it does not permanently save the preset.

The old `ble-status`, `ble-catalog`, `ble-backup`, and `ble-restore` spellings remain available as compatibility aliases. USB MIDI control is explicit, see [USB](#usb).

`ble-capture` records raw BLE-MIDI notifications and decoded MIDI/SysEx events. It is an investigation tool and sends no undocumented command automatically.

Pair `Nanocore Midi` with BlueZ once before using BLE readback. On Linux, the default `--backend auto` capture mode falls back to the legacy `gatttool` listener when Bleak cannot complete service discovery. Force either path with `--backend bleak` or `--backend gatttool`.

## USB

USB MIDI is explicit. Reads and writes use the ALSA raw MIDI device node (`/dev/snd/midiC<card>D<device>`), so your user needs access to it (the `audio` group on most systems); the `amidi` command is only used to discover the port. Reads, `backup`, `save` and `restore --apply` work over USB exactly as over Bluetooth, with the same safety rules (a new `--safety-backup` path is required before `save` and `restore --apply`):

```bash
nanocore --transport usb --port hw:6,0,0 preset 9
nanocore --transport usb status
nanocore --transport usb backup backups/my-preset.json
nanocore --transport usb save --safety-backup backups/before-save.json
```

Without `--port` the NANOCORE port is discovered with `amidi -l`. `preset` over USB sends a plain MIDI program change. Backups made over USB record `{"transport": "usb", "port": ...}` as their device. Which commands run over which transport:

| Command | Bluetooth | USB |
|---|---|---|
| `configure` | yes | yes |
| `devices` | yes | yes |
| `presets` | yes | yes |
| `status` | yes | yes |
| `preset` | yes | yes |
| `backup` | yes | yes |
| `restore` | yes | yes |
| `save` | yes | yes |
| `settings` | yes | yes |
| `rename` | yes | yes |
| `assets` | yes | yes |
| `next` | yes | yes |
| `previous` | yes | yes |
| `tuner` | yes | yes |
| `block` | yes | yes |
| `type` | yes | yes |
| `param` | yes | yes |
| `serve` | yes | yes |
| `ble-capture` | yes | no |

`ble-capture` is Bluetooth only and fails with a clear error on USB. After `block`, `type` and `param` the CLI reads the live state back with one cheap query to confirm the pedal answers.

## Pedal settings, names and models

```bash
nanocore settings                                  # Bluetooth, loopback, input gain, USB and Bluetooth volume, MIDI channel
nanocore settings --input-gain 2 --apply           # without --apply it only prints what it would send
nanocore rename Metal --safety-backup backups/before-rename.json    # active preset, up to 8 ASCII characters; stored
nanocore assets list                               # the 38 amplifier and 30 IR slots with names and CRC-32
nanocore assets read amp 12 backups/amp-12.bin     # one slot, verified by its CRC-32
nanocore assets backup backups/assets              # every slot plus a manifest (read only, about 7 s)
nanocore assets write ir 29 my-cab.wav --name MyCab --apply --safety-backup backups/ir-29-old.bin
```

`settings` changes pedal-wide settings that no preset holds (the pedal keeps them by itself). `rename` changes the name in RAM, saves the preset with the usual backup and reads the name back from the catalog. `assets write` replaces one slot: it first keeps the slot's current content in the new file you name, aborts the write session if any step fails, reads the slot back (length and CRC-32) and, when it was not the active one, selects the previous amplifier or IR again so the sound of the preset in use does not change. An IR can be a raw float32 file (up to 1024 samples) or a WAV, which is converted (mono, 48 kHz, first 1024 samples, peak 1.0; that conversion is this project's choice, see `docs/amp-ir-protocol.md`). An amplifier can be written with a blob that was read from a pedal. Livtra's encrypted `.ead` files are installed only through a decryptor that you provide (this project ships no key and no decryption): see `docs/ead-decryptor.md`. All of this was checked on a real pedal; see `docs/HARDWARE_VERIFICATION.md`.

## Logging and exit codes

`-v` logs the audit trail of every write to the pedal (command, slot, backup path, verification) to stderr; `-vv` logs everything under `nanocore`, including debug detail. Without `-v` nothing is logged. Only the CLI configures logging; the library never does.

Exit code `0` means success and `2` means an expected error, printed as `error: <message>`. A timeout on a write adds a line saying the command may have been applied and to check with `status`. A partly applied `restore --apply` prints the notes, how many operations were applied and the path of the safety backup. Tracebacks are never printed for expected errors.

## Local web server

```bash
nanocore --transport usb serve            # or: nanocore serve --transport bluetooth
# prints http://127.0.0.1:<port>/#token=...   open that address in a browser
```

`nanocore serve [options]` starts the local HTTP/WebSocket server that the web editor talks to. Connection options given before the word `serve` (`--transport`, `--address`, `--adapter`, `--port` for the ALSA port) are passed on to the server, and everything after it is the server's own, so `nanocore serve --help` lists them. It needs the `serve` extra (`uv sync --extra serve`) and a built editor (`./scripts/build_web.sh`; without one the server shows a placeholder page).

- It listens on `127.0.0.1` only and every request needs the token printed at start-up, which lives in the address you open and in that tab's session storage (never in local storage). `nanocore serve --no-token` turns it off, on the loopback address only; Host and Origin are still checked. Pages from other sites cannot talk to it.
- Edits reach the pedal at once but only live in its working memory. The save button next to the preset dropdown stores them in the pedal; the restore button goes back to the stored version. Switching preset with unsaved changes asks first. `--autosave` stores by itself after `--autosave-delay` idle seconds instead (at most one flash write every 10 seconds). Before the first edit of each preset the previous version is kept in `~/.local/share/nanocore-controller/baselines/`.
- `--read-only` turns off every write, including the autosave.
- The pedal accepts one Bluetooth connection at a time: close the phone app first. USB has no such limit.

The endpoints, error codes and security rules are in [`docs/api.md`](docs/api.md); what has and has not been verified on a real pedal is in [`docs/HARDWARE_VERIFICATION.md`](docs/HARDWARE_VERIFICATION.md).

## USB audio through the PC speakers

The NANOCORE is also a driver-free USB audio interface. `contrib/nanocore-loopback/` holds a PipeWire user service that routes its input to the PC speakers with low latency whenever the device is plugged in. The same service starts the web editor's server when the pedal is plugged in and opens it in the browser if no page has it open. Setup, tuning and troubleshooting are in [`docs/usb-audio-loopback.md`](docs/usb-audio-loopback.md).

## A MIDI controller for the pedal

The pedal's USB-C port and Bluetooth are device-only, so a footswitch board or a processor such as a BOSS GT-10 cannot be plugged into it. `contrib/midi-router/` is a small standard-library script for a Raspberry Pi or a computer that reads the controller and forwards its messages to the pedal, dropping the Bank Select that would make the pedal ignore Program Changes. See its README.

Development tests run with:

```bash
python -m pytest -q
```
