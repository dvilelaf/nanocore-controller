# Capturing the official app's traffic

Goal: learn the commands the official app uses for things this project cannot do yet (renaming
presets, global settings such as input and output level or loopback, changing what a chain holds).
The app talks to the pedal with the same SysEx frames as `docs/protocol.md`; Android can record that
traffic. Nothing here sends anything to the pedal: the app does, you only listen.

## Before you start

- **Do not update the firmware yet** (see the note at the end).
- Photograph the app's global settings screens, so every value can be put back.
- Close this project's server and the phone app, and connect the tablet to the pedal only.
- Do the experiments on one scratch preset. All 64 presets are saved in
  `~/nanocore-flash-backup-20261008/` (`nanocore backup` format).

## Record

1. On the tablet: Settings, About, tap *Build number* seven times to get Developer options.
2. Developer options: switch on **Enable Bluetooth HCI snoop log** (called *Bluetooth HCI snoop log*,
   set to *Enabled* or *Full*). Then switch Bluetooth off and on once.
3. Open the app, connect to the pedal, and do **one action at a time**, waiting about 10 seconds
   between actions and writing down the order and the values. For example:
   1. change input level from A to B, then back to A
   2. change output level from A to B, then back
   3. switch loopback on, then off
   4. rename the scratch preset to `AAAA`, then to `BBBB`
   5. in a chain block, change its type and switch it on and off (note what the app lets you add or remove)
   6. every other setting the app has, one at a time
4. Switch the snoop log off again.

## Get the file

- Developer options, **Take bug report** (full), share it to the PC; or connect with USB debugging and run
  `adb bugreport report.zip`.
- Inside the zip, the log is at `FS/data/misc/bluetooth/logs/btsnoop_hci.log` (older Android:
  `/sdcard/btsnoop_hci.log`).

## Limits found on a real tablet

On a Xiaomi tablet with Android 15 (no root) the bug report holds only `btsnooz_hci.log`: the short summary
the stack always keeps, with each packet cut to about 15 bytes. The headers show *that* the app wrote a
SysEx, not what it said, so it cannot be decoded, even with the snoop log set to *full*. The complete
`btsnoop_hci.log` lives in `/data/misc/bluetooth/logs/` and needs root. `adb bugreport` over Wi-Fi works
(`adb pair`, then connect) but takes about four minutes. Without root the other ways to learn the commands
are reading the app itself, or a computer standing in for the pedal and relaying to the real one.

## Read it

```bash
uv run python scripts/decode_btsnoop.py btsnoop_hci.log            # everything
uv run python scripts/decode_btsnoop.py btsnoop_hci.log --unknown  # only commands not in docs/protocol.md
```

Each line is one frame: time, direction, sequence, command, status and payload. Commands already known
(`0x63`, `0x40`, `0x36`, `0x56`, `0x45`, `0x6d`, `0x46`) are hidden by `--unknown`. Match the new ones with
your notes by time and order.

## Then

Each new command is documented in `docs/protocol.md` first, added to the library with a test, and tried
on a scratch preset after a backup, never on unknown payloads. Nothing is written to the pedal's
flash without a backup of it.

## A note on firmware

The app may offer a firmware update. Do not install it before the capture: the commands and the
parameter table measured in `docs/calibration/` belong to the current firmware, and an update can change
both. `docs/protocol.md` records that this firmware rejects the modern preset-record commands (`0x41`,
`0x42`), which may be exactly what a newer firmware adds. After an update, re-run the checks in
`docs/HARDWARE_VERIFICATION.md` and `scripts/calibrate_mapping.py`.
