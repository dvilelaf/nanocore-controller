# MIDI router: a controller (BOSS GT-10…) to a NANOCORE

The pedal's USB-C port and its Bluetooth are both device-only, so a MIDI controller cannot be plugged
into it. Something that can act as USB host (a Raspberry Pi, a laptop) has to sit in between.
`nanocore_midi_router.py` is that piece: standard library only, no installation.

```bash
./nanocore_midi_router.py --list                              # raw MIDI ports found
./nanocore_midi_router.py --config gt10.example.json --verbose # --input GT-10 --output Nanocore by default
```

It reads the controller through the ALSA sequencer (`aseqdump`, so PiPedal can use it at the same time)
and writes to the pedal's raw MIDI port. It waits for either to appear and reconnects after an unplug.
`--input-mode raw` opens the controller's raw node instead; only one program can hold that.

## What it does

| Controller sends | The pedal gets |
|---|---|
| Program Change n (bank 0) | Program Change n: preset n + 1 |
| Bank Select (CC 0, CC 32) | nothing. A bank other than 0 makes the pedal ignore every later Program Change until bank 0 comes back (measured) |
| a controller listed in `cc` | `pass` (same), `momentary` (renumbered), `toggle` (each press flips on/off) or `map` (scaled, for an expression pedal) |
| anything else | nothing |

`program_change.banks` gives, per bank, the number added to the program, so a bank the pedal does not
have can be moved onto presets; a bank that is not listed is ignored. `program_change.max` (63) drops
anything beyond the last preset.

## A GT-10

Measured with `aseqdump`: CTL1 sends CC 80, CTL2 CC 81, EXP SW CC 82 (127 on press, 0 on release), the
expression pedal CC 7 (continuous); each patch change sends CC 0, CC 32 and a Program Change numbered
along the whole list, so the four number buttons of the first page send 0 to 3 and the next page 4 to 7.
The pedal's own numbers do the same: CC 80 is the tuner, CC 81 and CC 82 previous and next preset.

The pedal's reaction to a Program Change, CC 81 or CC 82 takes about 100 ms.
