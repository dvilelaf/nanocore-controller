# Calibration of the runtime snapshot

The pedal's runtime snapshot (command `0x63`) and the documented MIDI controllers describe the same parameters in different ways. These files record how they relate, measured on a real pedal.

| File | What it is |
|---|---|
| `editor-catalog.json` | The editor's blocks, types and controller numbers, exported from `web/src/data` (an export, not a hand-written file). |
| `measurements.json` | Raw output of `scripts/calibrate_mapping.py`: for each block and type, which snapshot entry changed when each documented controller was sent. |
| `mapping.json` | The table derived from the measurements by `scripts/build_mapping.py`. The web editor reads a copy of it at `web/src/data/measuredMapping.json`. |

A test (`tests/test_build_mapping.py`) fails if `mapping.json` or the web copy no longer match `measurements.json`.

## Repeating the measurement

It changes only the live state of the active preset, never the stored one. It writes a backup first, restores the original state at the end (also after an error) and checks the restore. Nothing is sent without `--apply`, and the pedal must not be touched while it runs (a change of preset aborts it).

```bash
uv run python scripts/calibrate_mapping.py --backup /tmp/before.json            # plan only
uv run python scripts/calibrate_mapping.py --backup /tmp/before.json --apply    # about 2 minutes
uv run python scripts/build_mapping.py
```

Use a preset you do not mind hearing change for two minutes: types and parameters are switched all the time. Run the second command with the pedal on USB (`hw:N,0,0` is discovered with `amidi -l`). To refresh the catalog after changing the editor's data, export it again with a throwaway vitest test that writes the blocks, types and `activeParams` to `editor-catalog.json`.

## What was found

See "Measured behaviour of the live state" in `docs/protocol.md`.
