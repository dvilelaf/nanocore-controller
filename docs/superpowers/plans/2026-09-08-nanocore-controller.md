# NANOCORE Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a safe Python CLI that controls the LIVTRA NANOCORE over USB MIDI, discovers its BLE-MIDI data channel, and grows toward verified state readback and full preset backups.

**Architecture:** Keep MIDI encoding and protocol mappings pure and testable. Put USB/ALSA and BLE-MIDI behind transport interfaces, and keep observed, sent, and assumed state separate. Use a safety layer that never saves or restores without explicit confirmation and never calls firmware operations.

**Tech Stack:** Python 3.11+, `argparse`, `subprocess`/ALSA `amidi`, optional `bleak` for BLE, `unittest` or pytest-compatible tests, JSON backups with SHA-256 checksums.

---

## Repository layout

- `src/nanocore_controller/midi.py`: MIDI channel/status-byte and CC/PC encoding.
- `src/nanocore_controller/mappings.py`: documented NANOCORE CC/type/parameter tables and aliases.
- `src/nanocore_controller/transport.py`: transport protocol plus USB `amidi` implementation and fake transport for tests.
- `src/nanocore_controller/discovery.py`: ALSA port discovery and BLE device/service discovery.
- `src/nanocore_controller/state.py`: observed/sent/assumed state model and JSON snapshot/backup serialization.
- `src/nanocore_controller/cli.py`: command parsing, validation, safety confirmations, and command dispatch.
- `src/nanocore_controller/__main__.py`: `python -m nanocore_controller` entry point.
- `tests/test_midi.py`: byte encoding tests.
- `tests/test_mappings.py`: mapping, aliases, validation, and compatibility tests.
- `tests/test_state.py`: snapshot, backup, checksum, and diff tests.
- `tests/test_cli.py`: dry-run and fake-transport CLI tests.
- `tests/fixtures/`: captured BLE-MIDI packets once readback discovery produces them.
- `README.md`: installation, examples, safety notes, and current protocol limitations.
- `pyproject.toml`: package metadata, optional BLE dependency, test command, and console script.
- `docs/protocol.md`: verified USB mappings and progressively decoded BLE state protocol.

### Task 1: Bootstrap the Python package and test harness

**Files:**
- Create: `pyproject.toml`
- Create: `src/nanocore_controller/__init__.py`
- Create: `tests/test_midi.py`
- Create: `README.md`

- [ ] **Step 1: Write the failing package smoke test**

```python
from nanocore_controller import __version__


def test_package_exposes_version():
    assert __version__ == "0.1.0"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest discover -s tests -v`
Expected: FAIL because the package and test runner configuration do not exist yet.

- [ ] **Step 3: Add minimal package metadata and version**

Define `nanocore-controller` as a Python 3.11 package, expose `__version__ = "0.1.0"`, and configure the `nanocore` console script to call `nanocore_controller.cli:main`.

- [ ] **Step 4: Run the smoke test**

Run: `python -m unittest discover -s tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add pyproject.toml src tests README.md && git commit -m "chore: bootstrap nanocore controller"`

### Task 2: Implement pure MIDI encoding first

**Files:**
- Create: `src/nanocore_controller/midi.py`
- Modify: `tests/test_midi.py`

- [ ] **Step 1: Write failing encoding tests**

Cover MIDI channel 1 as status-byte channel 0, channels 1-16, CC values 0-127, Program Change values 0-127, and rejection of out-of-range values. Expected examples:

```python
from nanocore_controller.midi import control_change, program_change


def test_cc_on_midi_channel_one():
    assert control_change(1, 80, 127) == bytes.fromhex("b0 50 7f")


def test_program_change_on_channel_two():
    assert program_change(2, 12) == bytes.fromhex("c1 0c")


def test_cc_rejects_invalid_values():
    with pytest.raises(ValueError):
        control_change(1, 80, 128)
```

Use the standard library test framework or add pytest as a development dependency; do not make tests require hardware.

- [ ] **Step 2: Run tests and verify the expected failures**

Run: `python -m unittest discover -s tests -v` (or `python -m pytest -q` if pytest is configured).
Expected: FAIL because `midi.py` is absent.

- [ ] **Step 3: Implement minimal encoders**

Implement `control_change(channel: int, controller: int, value: int) -> bytes` as `0xB0 | (channel - 1), controller, value`, and `program_change(channel: int, program: int) -> bytes` as `0xC0 | (channel - 1), program`. Validate channel 1-16 and all 7-bit fields.

- [ ] **Step 4: Run the MIDI tests**

Expected: all encoding and validation tests PASS.

- [ ] **Step 5: Commit**

Run: `git add src/nanocore_controller/midi.py tests/test_midi.py && git commit -m "feat: add MIDI CC and program change encoding"`

### Task 3: Add the documented NANOCORE mappings

**Files:**
- Create: `src/nanocore_controller/mappings.py`
- Create: `tests/test_mappings.py`
- Create: `docs/protocol.md`

- [ ] **Step 1: Write mapping tests**

Test that `reverb` uses on/off CC 27 and type CC 47, `amp_gain` uses CC 60, `cab_level` uses CC 67, `shimmer` resolves to Reverb type ID 4, and `percent_to_midi(50)` returns 64. Test that unsupported parameter/block combinations are rejected.

- [ ] **Step 2: Run tests and verify they fail**

Expected: FAIL because mappings are absent.

- [ ] **Step 3: Implement declarative mappings**

Encode the official guide's CC map: FX1 20/40, FX2 21/41, Amp 23/43, Cab 24/44, Mod 25/45, Delay 26/46, Reverb 27/47, EQ 28/48; global tuner 80, previous 81, next 82; direct parameters 50-79 and EQ 91-97/33. Include type aliases for all documented effect types. Keep amp/cab type IDs numeric-only until their exact indexing is verified.

- [ ] **Step 4: Run tests and document verified mappings**

Expected: mapping tests PASS. `docs/protocol.md` must state that the guide exposes CC43/CC44 but does not publish their model/IR slot table.

- [ ] **Step 5: Commit**

Run: `git add src/nanocore_controller/mappings.py tests/test_mappings.py docs/protocol.md && git commit -m "feat: add documented nanocore control mappings"`

### Task 4: Implement USB transport and robust device discovery

**Files:**
- Create: `src/nanocore_controller/transport.py`
- Create: `src/nanocore_controller/discovery.py`
- Create: `tests/test_transport.py`
- Create: `tests/test_discovery.py`

- [ ] **Step 1: Write fake-transport and discovery tests**

Assert that a fake transport records exact bytes and that discovery selects the `hw:<card>,<device>,<subdevice>` port whose `amidi -l` output contains `Nanocore MIDI 1`, rather than assuming card 6 forever. Test clear errors for missing `amidi` and missing NANOCORE.

- [ ] **Step 2: Run tests and verify they fail**

Expected: FAIL because transport/discovery modules do not exist.

- [ ] **Step 3: Implement transport**

Define a `MidiTransport` protocol with `send(payload: bytes) -> None`. Implement `AmidiTransport` using `subprocess.run(["amidi", "-p", port, "--send-hex", hex_string], check=True, capture_output=True, text=True)`. Never invoke `amidi` with firmware or file-upload arguments.

- [ ] **Step 4: Implement discovery**

Parse `amidi -l`, select a port whose device name contains `Nanocore MIDI`, and expose a diagnostic listing of ALSA MIDI/audio endpoints. Keep explicit `--port` override support.

- [ ] **Step 5: Run tests and a read-only hardware discovery**

Run: `python -m unittest discover -s tests -v`; then `amidi -l` and `aconnect -l` against the attached pedal. Expected: tests PASS and the live discovery reports `Nanocore MIDI 1`.

- [ ] **Step 6: Commit**

Run: `git add src/nanocore_controller/transport.py src/nanocore_controller/discovery.py tests/test_transport.py tests/test_discovery.py && git commit -m "feat: add USB MIDI transport and discovery"`

### Task 5: Add the safe CLI write path and dry-run mode

**Files:**
- Create: `src/nanocore_controller/cli.py`
- Create: `src/nanocore_controller/__main__.py`
- Create: `tests/test_cli.py`
- Modify: `README.md`

- [ ] **Step 1: Write CLI tests**

Test `--dry-run tuner on` prints `b0 50 7f` and sends nothing; `next` prints `b0 52 7f`; `preset 12` prints `c0 0c`; `reverb type shimmer` prints `b0 2f 04`; and invalid values exit nonzero without sending.

- [ ] **Step 2: Run tests and verify they fail**

Expected: FAIL because no CLI exists.

- [ ] **Step 3: Implement command parsing and dispatch**

Add `devices`, `preset`, `next`, `previous`, `tuner`, `block`, `type`, and `param` commands. Use the mapping layer to create bytes and transport to send them. Use `--dry-run` to print bytes. Default to channel 1 and auto-discovered USB port.

- [ ] **Step 4: Run tests and verify the CLI**

Expected: all CLI tests PASS. With the attached device, run `python -m nanocore_controller devices`, then the already verified reversible commands `tuner on` and `next`.

- [ ] **Step 5: Commit**

Run: `git add src/nanocore_controller/cli.py src/nanocore_controller/__main__.py tests/test_cli.py README.md && git commit -m "feat: add safe nanocore command line control"`

### Task 6: Add state model, snapshots, checksums, and guarded restore primitives

**Files:**
- Create: `src/nanocore_controller/state.py`
- Create: `tests/test_state.py`
- Modify: `src/nanocore_controller/cli.py`

- [ ] **Step 1: Write state and backup tests**

Test that observed, sent, and assumed values serialize separately; a snapshot includes device identity and firmware fields when known; checksum changes when payload changes; restore defaults to dry-run; and `--confirm` is required before applying more than one preset update.

- [ ] **Step 2: Run tests and verify they fail**

Expected: FAIL because state/backup code does not exist.

- [ ] **Step 3: Implement JSON serialization and diff**

Use stable sorted JSON, UTC timestamps, and SHA-256 over the canonical payload. Implement `save_snapshot`, `load_snapshot`, and a human-readable diff. Label partial sent-state snapshots as non-backups.

- [ ] **Step 4: Implement guarded CLI commands**

Add `snapshot`, `backup`, and `restore`. `restore` must print its diff and refuse to send unless `--confirm` is present. Before any multi-message write, attempt a full backup and abort if only an incomplete sent-state snapshot is available.

- [ ] **Step 5: Run tests**

Expected: all state and existing tests PASS.

- [ ] **Step 6: Commit**

Run: `git add src/nanocore_controller/state.py src/nanocore_controller/cli.py tests/test_state.py && git commit -m "feat: add safe snapshots and guarded restore"`

### Task 7: Implement BLE-MIDI capture and readback discovery

**Files:**
- Create: `src/nanocore_controller/ble_midi.py`
- Create: `tests/test_ble_midi.py`
- Create: `tests/fixtures/README.md`
- Modify: `src/nanocore_controller/discovery.py`
- Modify: `docs/protocol.md`

- [ ] **Step 1: Write BLE-MIDI framing tests**

Test timestamp/header stripping, packet fragmentation, running-status decoding, CC/PC decoding, and preservation of SysEx payloads using fixed byte fixtures. Do not require Bluetooth hardware for these tests.

- [ ] **Step 2: Run tests and verify they fail**

Expected: FAIL because BLE framing/decoder code is absent.

- [ ] **Step 3: Implement BLE-MIDI framing and optional Bleak transport**

Use the discovered service UUID `03b80e5a-ede8-4b33-a751-6ce34ec4c700` and characteristic UUID `7772e5db-3868-4112-a1a9-f2669d106bf3`. Make `bleak` optional so USB-only usage remains lightweight. Subscribe to notifications and expose raw packet callbacks.

- [ ] **Step 4: Capture the iOS synchronization exchange**

With the iOS app disconnected from the device, connect the tool to `Nanocore Midi`, capture notifications, and then make one controlled change in the iOS app at a time. If a handshake is required, record the exact outbound/inbound packets and keep them in fixtures. Do not send undocumented writes except a reversible state query or a previously verified handshake packet.

- [ ] **Step 5: Implement readback decoding**

Decode the observed state snapshot and incremental updates into the `observed` state model. Verify with preset 09 and one parameter change, then document confirmed message formats and any remaining unknowns.

- [ ] **Step 6: Commit**

Run: `git add src/nanocore_controller/ble_midi.py src/nanocore_controller/discovery.py tests/test_ble_midi.py tests/fixtures docs/protocol.md && git commit -m "feat: add BLE MIDI capture and readback"`

### Task 8: Implement full backup/restore from verified readback

**Files:**
- Modify: `src/nanocore_controller/state.py`
- Modify: `src/nanocore_controller/cli.py`
- Create: `tests/test_backup_restore.py`
- Modify: `README.md`

- [ ] **Step 1: Write full-backup tests**

Test round-tripping all 64 preset slots, retaining names and amp/cab/IR references, rejecting a backup missing slots, and verifying that restore diffs only the requested slots.

- [ ] **Step 2: Run tests and verify they fail**

Expected: FAIL because full device backup support is not yet implemented.

- [ ] **Step 3: Implement complete backup format**

Require an observed device-originated snapshot for `backup`; include timestamp, USB identity, firmware, raw packets, normalized presets, and SHA-256 checksum. Refuse to label a sent-state-only file as a full backup.

- [ ] **Step 4: Implement restore transaction**

Before restore, create a fresh backup, show the diff, require `--confirm`, send the minimal required messages, and read back each changed slot. If verification fails, report the affected slots and do not claim success.

- [ ] **Step 5: Run the full suite and hardware smoke test**

Run: `python -m unittest discover -s tests -v`; then back up the device, change preset 09 temporarily, restore only that slot with explicit confirmation, and verify the displayed preset/state.

- [ ] **Step 6: Commit**

Run: `git add src tests README.md docs/protocol.md && git commit -m "feat: add verified full preset backups and restore"`

## Plan self-review

- USB control, all documented mappings, device discovery, dry-run, and safe writes are covered by Tasks 2-5.
- Observed/sent/assumed state, checksums, diffs, and guarded restore are covered by Tasks 6 and 8.
- BLE service discovery, notification capture, packet decoding, and iOS synchronization are covered by Task 7.
- Amp/cab model indexing remains explicitly experimental until verified; the plan does not invent IDs.
- Firmware is intentionally out of scope for the controller and no task can invoke an updater.
- No placeholders or unowned “handle edge cases” steps are used; every implementation task identifies files, tests, commands, and expected outcomes.
