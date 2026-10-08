# NANOCORE protocol notes

## USB MIDI

The attached device enumerates as `Ember Nanocore` (`33c3:1301`) and exposes `Nanocore MIDI 1`. The MIDI guide is compatible with firmware 1.04+.

### Block controls

| Block | On/off CC | Type CC |
| --- | ---: | ---: |
| FX1 | 20 | 40 |
| FX2 | 21 | 41 |
| Amp | 23 | 43 |
| Cab | 24 | 44 |
| Mod | 25 | 45 |
| Delay | 26 | 46 |
| Reverb | 27 | 47 |
| EQ | 28 | 48 |

On/off values 0-63 mean off and 64-127 mean on. Type values are exact IDs, not a scaled knob value.

### Global controls

- CC80: tuner off/on
- CC81: previous preset
- CC82: next preset
- Program Change 0-127: direct preset recall

### Direct parameters

- CC50-56: FX1 gate/compressor
- CC57-59: FX2 drive/boost
- CC60-64: amp gain, bass, mid, treble, level
- CC65-67: cab low cut, high cut, level
- CC68-73: modulation or FX2 pitch/wah, depending on the selected type
- CC74-79: delay, depending on the selected type
- CC85-90: reverb, depending on the selected type
- CC91-97 and CC33: EQ bands

The guide publishes the amp/cab type controller numbers but does not publish a model/profile or IR-slot table. The controller therefore accepts numeric amp/cab IDs without claiming a name mapping until verified.

## BLE-MIDI

The device advertises a separate `Nanocore Midi` data endpoint. The observed service and characteristic are:

- Service: `03b80e5a-ede8-4b33-a751-6ce34ec4c700`
- MIDI data characteristic: `7772e5db-3868-4112-a1a9-f2669d106bf3`
- CCC descriptor observed at handle `0x0014` during this session

The endpoint requires normal BLE pairing. With the pedal paired, subscribing before writing works reliably through Bleak on `hci1`; no empty handshake write is required.

Application requests are SysEx messages beginning `f0 7d 4e 43 70`; responses use direction byte `71`. The body is encoded in groups of seven bytes using a leading high-bit mask. The decoded request body is:

```text
02 | sequence:u16le | command:u16le | payload_length:u16le | payload
```

The decoded response inserts `status:u8` before the payload length. Statuses `10` and `11` carry intermediate and final chunks respectively; each chunk starts with total length and offset as little-endian `u16` values.

Verified read-only commands:

- `0x63`: active runtime snapshot (preset, volume, eight effects, parameters, chain order)
- `0x40 [start,count]`: preset-name catalog page; this firmware returns at most six entries
- `0x36 [slot]`: amplifier-slot metadata; byte 2 marks the active slot
- `0x56 [slot]`: cabinet/IR-slot metadata; byte 2 marks the active slot
- `0x45`: preset-storage capabilities; this unit returns the legacy marker `00`

Commands `0x41`/`0x42` are associated with modern 128-byte `RSP1` preset records, but this firmware rejects `0x41`. The controller does not attempt preset-record migration or firmware operations. The exact raw `0x63` payload is retained in every backup and cross-checked before a live restore.

Verified live-write command `0x6d` uses a field byte followed by field data:

- `01 effect_index parameter_index float32le`: one normalized parameter
- `02 effect_index variant count float32le[count]`: variant and complete parameter array
- `03 effect_index enabled`: block enabled state
- `04 volume`: preset volume, 0-100
- `05 08 order[8]`: effect-chain permutation
- `06 slot`: amplifier slot
- `07 slot`: cabinet/IR slot
- `08 length characters`: name of the active preset, 1 to 8 printable ASCII characters (trailing spaces are dropped; the pedal pads names with spaces). The pedal answers with the same bytes. It only changes the working copy: the catalog (`0x40`) shows the new name after the preset is saved with `0x46`. Found in the official app (it checks the echo and calls this a live edit).

The live `effect_index` is the zero-based position in the eight-entry runtime snapshot. It is not the unrelated `effectId` (1-8) stored in exported preset JSON; sending that value caused `effectId=8` to be rejected with status `0x03`. A same-state restore of preset 09 was confirmed on hardware by applying all 20 operations and reading runtime, amplifier and IR state back successfully. These writes affect live state only and do not invoke permanent preset save.

### Preset recall

The official ToneCommand app's native helper at `0x357da8` contains a command `0x76`
branch for NanoCore device type 7. Its two-byte payload is `09 slot`, where `slot` is
zero-based (`visible preset number = slot + 1`) and ranges from 0 through 127.
For example, visible preset 9 uses payload `09 08`. This command has no checksum;
the apparent extra bytes in the SysEx body are the normal 7-bit packing masks.

Physical testing against this unit on firmware 1.04 showed that it rejects the
`0x76` variant with status `0x7e`. The compatible branch sends the same `09 slot`
payload through command `0x6d`; `0x6d 09 07` recalled visible preset 8 and a `0x63`
readback confirmed active slot 7. The controller therefore uses `0x6d` for firmware
1.04 while retaining the decoded `0x76` framing as protocol evidence.

### Permanent preset save

ToneCommand operation 23 dispatches firmware 1.04 preset storage to command
`0x46` with a one-byte, zero-based slot payload. On this pedal, `0x46 08`
successfully stored the live state in visible preset 9. Persistence was verified
by changing to preset 8, returning to preset 9, and observing the edited amp gain;
the original backup was then restored, saved again, and matched byte-for-byte
after another preset round trip.
With sequence 1, the complete SysEx frame for visible preset 9 is:

```text
f0 7d 4e 43 70 00 02 01 00 76 00 02 00 00 09 08 f7
```

## Measured behaviour of the live state

Measured on a real pedal with `scripts/calibrate_mapping.py` (693 documented controllers sent to the active preset, live state only, original state restored and verified byte for byte). Raw data: `docs/calibration/measurements.json`; derived table: `docs/calibration/mapping.json`. See `docs/calibration/README.md` to repeat it.

### Order of the runtime snapshot

The eight entries of command `0x63` are, in order: `fx1, fx2, amp, cab, del, mod, rev, eq`. This is not the editor's block numbering, which has `mod` before `del`. It was found from each block's on/off controller (CC 20, 21, 23, 24, 26 for the delay, 25 for modulation, 27, 28): toggling it flips the `enabled` flag of exactly one entry.

### Parameter positions and controllers

Parameter `j` of an entry is changed by the documented controller `base + j`, with these bases: FX1 50 (the compressor type starts at 51), FX2 drive and boost 57, FX2 pitch and wah 68, amp 60, cab 65, modulation 68, delay 74, reverb 85, EQ 91 (the 8-band EQ also maps CC 33 to parameter 7). Values are linear: `normalized = controller / 127`; stepped parameters are quantized to `index / (steps - 1)`.

Some snapshot parameters have no documented controller (Gate and Auto Gate have two parameters, the 8-band EQ has a ninth), and some controllers have no snapshot parameter, which is why the editor lists more parameters than the pedal stores for the delay, modulation, reverb and cab (their `level` controllers: CC 78 for BBD, 67, 90...). `docs/calibration/mapping.json` is the authoritative table per block and type.

Controllers 68 to 73 are shared: they drive FX2 when its type is Pitch, Envelope Wah, Wah or Motion Wah, and the modulation block otherwise.

### Global settings (commands `0x65` and `0x66`)

Settings that belong to the pedal and not to a preset: its own Bluetooth, loopback, input gain, the USB and Bluetooth volumes and the MIDI channel. They were found in the official app (its native library builds and reads these frames) and the read was checked on a pedal.

`0x65` with an empty payload answers with the settings; `0x66` changes some of them and answers with the new settings in the same layout.

Reply layout (7 bytes; version 2 and later add two more):

| Byte | Meaning |
|---|---|
| 0 | layout version (1 on the pedal used here) |
| 1 | Bluetooth on (0 or 1) |
| 2 | loopback on (0 or 1) |
| 3 | input gain in dB, signed, -20 to 20 |
| 4 | USB volume, 0 to 100 |
| 5 | Bluetooth volume, 0 to 100 |
| 6 | MIDI channel, 0 (omni) to 16 |
| 7, 8 | version 2 only: lower and upper volume limits |

A real reply with the input gain set to +1 in the app was `01 01 00 01 64 64 00`.

`0x66` payload: a **one-byte** field mask, then the value of each chosen field in mask order: bit 0x01 Bluetooth, 0x02 loopback, 0x04 input gain (signed), 0x08 USB volume, 0x10 Bluetooth volume, 0x20 MIDI channel (bits 0xc0 add the two volume limits of the version-2 layout). Changing the input gain to +2 and back to +1 was done on a pedal and read back. A 15-byte reply with a five-band global equalizer and a **two-byte** mask belongs to another product code and is not implemented. Sending that two-byte mask to this pedal makes it read the second byte as a value: `04 00 02` set the input gain to 0 and was answered with status `0x01`.

Other commands the app uses and this project does not (yet) know: `0x41` and `0x42` (preset records, which hold the name), `0x01`, `0x30` to `0x38`, `0x3b`, `0x50` to `0x5a` (chunked upload of amps and impulse responses), `0x6e`, `0x75`, `0x78`, `0x79`, `0x81`. A third frame direction byte, `0x72`, carries notifications from the pedal.

### Status codes

A command the pedal does not know is answered with status `0x7e` (126). Other statuses, as the official app words them: `1` bad payload (also a payload of the wrong length), `2` no active write session, `3` slot out of range, `4` flash read/write failed, `5` CRC mismatch.

### Timing

Selecting a preset loads its amplifier and IR in 0.05 to 0.3 s. Writing the amplifier or IR slot directly (`06` and `07`) takes about 0.5 s to show as active, so a read-back right after the write can still show the old slot; the restore code polls for up to three seconds.

On one occasion, after recalling preset 64 with a Program Change and waiting a second, the live effects were those of preset 64 but the amplifier and IR were still those of the previous preset, and saving then stored the wrong amp and IR in preset 64 (restored afterwards from a backup). It was not reproduced in a dozen later recalls. Before saving a preset right after recalling it, check that the amplifier and IR are the ones the preset should have.

### Types and variants

The type controller of FX1, FX2, modulation, delay, reverb and EQ takes the editor's type id, and the pedal reports it back as `variant`. The ids are the same except for reverb: type ids 0 to 6 give variants 0, 1, 2, 3, 5, 6, 7 (Shimmer is variant 5, Cloud 6, Spring 7; variant 4 does not exist). The type controllers of the amplifier and the cabinet change nothing; their models are selected with the live fields `06` and `07` (SysEx).

### Timing

A live write is acknowledged before it shows in the snapshot. A read taken at once can return the previous state; waiting about 20 ms (the largest delay seen was 0.02 s, with a 0.02 s polling step) was enough every time. Code that compares a read-back with what it wrote must poll until two consecutive reads agree.

### Chain order

Writing the chain order (live field `05`) and reading the snapshot back returned exactly the values written, after the delay above. **A value is the position of an entry of the snapshot** (`fx1, fx2, amp, cab, del, mod, rev, eq` = 0 to 7) and its position in the list is its place in the signal path. Checked against the official app's chain screen on a pedal: `[0,1,2,3,4,5,6,7]` shows FX1 FX2 AMP CAB DEL MOD REV EQ; `[0,1,2,3,5,4,6,7]` shows MOD before DEL; `[7,6,5,4,3,2,1,0]` shows EQ REV MOD DEL CAB AMP FX2 FX1.

### Firmware layouts

The factory preset file shipped with the official app lists one parameter for Gate and none for Auto Gate, and one more parameter than this pedal reports for Pitch, Envelope Wah and Wah. The measured table therefore belongs to the firmware of the pedal it was measured on. The web editor compares the length of the parameter array with the measured table and hides the parameters of a block that disagrees.

## Master volume and level meters

The MASTER knob and the input and output signal meters of the pedal's screen are not part of the protocol the official app uses with this pedal. Checked on 2026-10-08: with the knob turned and a guitar played, neither the `0x63` snapshot, nor `0x65`, nor any message sent by the pedal changed; and the app's own screens (global settings: loopback, input gain, USB volume, Bluetooth volume, MIDI channel, and a language setting that changes only the app (the pedal's own menus stay as they were); the preset's volume slider next to upload and download; a Playground tab whose stem mixer has a master slider, disabled, for audio played inside the app) show no meter and no master control for the pedal. The editor therefore has the preset volume and the global volumes, and nothing for these two.

