# Bluetooth-Only Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every currently supported NANOCORE operation work through one Bluetooth connection by default, with USB available only when explicitly selected.

**Architecture:** Add an XDG-backed device profile and a `BluetoothController` facade around one `BleMidiSession`. Refactor state and restore helpers to accept an existing session, then route the CLI through exactly one selected transport. Use the official private preset-recall command `0x76` with payload `09 <zero-based slot>` and verify recall through runtime command `0x63`.

**Tech Stack:** Python 3.11, argparse, asyncio, Bleak/BLE-MIDI, pytest/unittest, ALSA `amidi` for optional USB.

---

### Task 1: Official BLE preset-recall protocol

**Files:**
- Modify: `src/nanocore_controller/nanocore_protocol.py`
- Modify: `docs/protocol.md`
- Test: `tests/test_nanocore_protocol.py`

- [ ] **Step 1: Write failing protocol tests**

```python
def test_builds_official_select_preset_payload(self):
    self.assertEqual(select_preset_payload(8), bytes.fromhex("09 08"))

def test_rejects_select_preset_slot_outside_nanocore_range(self):
    for slot in (-1, 128):
        with self.subTest(slot=slot), self.assertRaises(ValueError):
            select_preset_payload(slot)

def test_encodes_official_preset_9_request(self):
    request = encode_request(SELECT_PRESET_COMMAND, select_preset_payload(8), sequence=1)
    self.assertEqual(
        request,
        bytes.fromhex("f0 7d 4e 43 70 00 02 01 00 76 00 02 00 00 09 08 f7"),
    )
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_nanocore_protocol.py`

Expected: import failures for `SELECT_PRESET_COMMAND` and `select_preset_payload`.

- [ ] **Step 3: Implement the byte-exact command**

```python
SELECT_PRESET_COMMAND = 0x76

def select_preset_payload(slot: int) -> bytes:
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < 128:
        raise ValueError("preset slot must be an integer from 0 to 127")
    return bytes((0x09, slot))
```

Document the disassembled ToneCommand evidence, zero-based range, exact SysEx example, and absence of a checksum in `docs/protocol.md`.

- [ ] **Step 4: Run focused tests and commit**

Run: `.venv/bin/pytest -q tests/test_nanocore_protocol.py`

Expected: all protocol tests pass.

```bash
git add src/nanocore_controller/nanocore_protocol.py tests/test_nanocore_protocol.py docs/protocol.md
git commit -m "feat: encode official BLE preset recall"
```

### Task 2: One-session Bluetooth controller

**Files:**
- Create: `src/nanocore_controller/controller.py`
- Modify: `src/nanocore_controller/ble_state.py`
- Modify: `src/nanocore_controller/ble_restore.py`
- Test: `tests/test_controller.py`
- Modify: `tests/test_ble_state.py`
- Modify: `tests/test_ble_restore.py`

- [ ] **Step 1: Write failing controller tests**

Use a fake session that records `connect`, `send`, `query`, and `close`. Cover:

```python
async with BluetoothController(ADDRESS, adapter="hci1", session_factory=Session) as pedal:
    result = await pedal.select_preset(9)
self.assertEqual(session.calls[0], ("query", 0x76, bytes.fromhex("09 08")))
self.assertEqual(result.active_preset, 8)
self.assertEqual(session.connect_count, 1)
self.assertEqual(session.close_count, 1)
```

Also test a mismatching `0x63` readback raises `RuntimeError`, and `send_midi(bytes.fromhex("b0 50 7f"))` uses that same session.

- [ ] **Step 2: Run focused tests and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_controller.py tests/test_ble_state.py tests/test_ble_restore.py`

Expected: `BluetoothController` and session-based helper imports fail.

- [ ] **Step 3: Extract session-based state and restore helpers**

Add `read_preset_catalog_from_session(session)`, `read_active_state_from_session(session,
address, adapter: str)`, and `apply_restore_with_session(document, session)`. Move the existing
query loops and `try/finally`-independent logic into those functions without changing command
bytes, retry counts, validation, or readback comparisons.

Keep the existing public wrappers; each wrapper connects once, calls its session helper, and closes in `finally`.

- [ ] **Step 4: Implement the controller facade**

Implement `BluetoothController(address, adapter="hci1", session_factory=BleMidiSession)` as an
async context manager. `__aenter__` connects its one session and returns the controller;
`__aexit__` always closes it. Its `status`, `presets`, and `restore` methods call the session-based
helpers. `send_midi` rejects an empty message and delegates to `session.send`.

`select_preset(9)` validates 1–128, queries `0x76` with `09 08`, then queries `0x63` and requires `active_preset == 8`. The controller never imports discovery or `AmidiTransport`.

- [ ] **Step 5: Run focused and full tests, then commit**

Run: `.venv/bin/pytest -q tests/test_controller.py tests/test_ble_state.py tests/test_ble_restore.py && .venv/bin/pytest -q`

Expected: all tests pass.

```bash
git add src/nanocore_controller/controller.py src/nanocore_controller/ble_state.py src/nanocore_controller/ble_restore.py tests/test_controller.py tests/test_ble_state.py tests/test_ble_restore.py
git commit -m "feat: add single-session Bluetooth controller"
```

### Task 3: Persisted Bluetooth device profile

**Files:**
- Create: `src/nanocore_controller/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing configuration tests**

Cover the XDG path, fallback path, strict schema/address validation, default adapter, exclusive data shape, and atomic replacement:

```python
profile = DeviceProfile(address="AA:BB:CC:DD:EE:FF", adapter="hci1")
save_profile(profile, path)
self.assertEqual(load_profile(path), profile)
self.assertEqual(
    json.loads(path.read_text()),
    {"version": 1, "address": "AA:BB:CC:DD:EE:FF", "adapter": "hci1"},
)
```

- [ ] **Step 2: Run tests and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_config.py`

Expected: module import failure.

- [ ] **Step 3: Implement strict, atomic profile storage**

Create immutable `DeviceProfile(address: str, adapter: str = "hci1")` plus
`default_config_path`, `load_profile`, and `save_profile`. `default_config_path` reads
`XDG_CONFIG_HOME` or uses `Path.home() / ".config"`; `load_profile` requires exactly schema
version 1, address, and adapter; `save_profile` serializes those three fields.

Validate a six-octet colon-separated Bluetooth address and a non-empty adapter. Write a temporary file in the destination directory, flush it, and replace the destination atomically.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/pytest -q tests/test_config.py`

Expected: all configuration tests pass.

```bash
git add src/nanocore_controller/config.py tests/test_config.py
git commit -m "feat: store default Bluetooth device"
```

### Task 4: Bluetooth-default CLI and explicit USB

**Files:**
- Modify: `src/nanocore_controller/cli.py`
- Modify: `src/nanocore_controller/mappings.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_mappings.py`

- [ ] **Step 1: Write failing CLI routing tests**

Cover:

```python
code = run(["--config", str(config), "preset", "9"], output=output)
controller.select_preset.assert_awaited_once_with(9)
usb_discovery.assert_not_called()
```

Add cases for `configure`, `presets`, `status`, `backup`, `restore`, every live MIDI command, `--address` override, and `--transport usb`. Assert default BLE paths never instantiate `AmidiTransport`, explicit USB paths never instantiate `BluetoothController`, Bluetooth errors never trigger USB fallback, and preset 9 over USB sends `c0 08`.

- [ ] **Step 2: Write failing numeric amp/cab type tests**

```python
self.assertEqual(resolve_type("amp", "4"), 4)
self.assertEqual(resolve_type("cab", "12"), 12)
```

Reject numeric strings outside 0–127.

- [ ] **Step 3: Run focused tests and confirm they fail**

Run: `.venv/bin/pytest -q tests/test_cli.py tests/test_mappings.py`

Expected: new public commands/options are unknown and numeric amp/cab IDs fail.

- [ ] **Step 4: Implement parser and routing**

Add global `--transport {bluetooth,usb}` (default `bluetooth`), `--address`, `--adapter`, and test-only/documented `--config`. Add `configure`, `presets`, `status`, `backup`, and `restore`; retain `ble-catalog`, `ble-status`, `ble-backup`, and `ble-restore` as aliases.

Resolve the selected transport once:

```python
if args.transport == "usb":
    # MIDI commands only; no Bluetooth construction.
    payload = command_payload(args, preset_base=1)
    selected = transport or AmidiTransport(args.port or discover_nanocore_midi_port())
    selected.send(payload)
else:
    profile = resolve_profile(args)
    asyncio.run(run_bluetooth_command(args, profile, controller_factory))
```

For Bluetooth live MIDI commands, use `await controller.send_midi(payload)` and read runtime state afterward when exposed. For preset recall, use `await controller.select_preset(args.number)`, not Program Change. Restore must create the safety backup before the first write and call `controller.restore` in the same context.

- [ ] **Step 5: Implement display-number mapping and numeric types**

```python
if args.command == "preset":
    if not 1 <= args.number <= 128:
        raise ValueError("preset number must be from 1 to 128")
    return program_change(args.channel, args.number - 1)
```

In `resolve_type`, parse decimal strings as IDs before named lookup so amp/cab numeric IDs work.

- [ ] **Step 6: Run focused/full tests and commit**

Run: `.venv/bin/pytest -q tests/test_cli.py tests/test_mappings.py && .venv/bin/pytest -q`

Expected: all tests pass and transport-isolation mocks show no parallel connection.

```bash
git add src/nanocore_controller/cli.py src/nanocore_controller/mappings.py tests/test_cli.py tests/test_mappings.py
git commit -m "feat: make Bluetooth the default transport"
```

### Task 5: Documentation and physical BLE-only verification

**Files:**
- Modify: `README.md`
- Modify: `docs/protocol.md`

- [ ] **Step 1: Update user documentation**

Document:

```bash
nanocore configure AA:BB:CC:DD:EE:FF --adapter hci1
nanocore presets
nanocore status
nanocore preset 9
nanocore backup backups/preset-09.json
nanocore block reverb on
nanocore type amp 4
nanocore param amp_gain 70%
nanocore --transport usb --port hw:6,0,0 preset 9
```

State clearly that Bluetooth is the default, USB is optional, there is no fallback, edits are live-only, and permanent save/firmware update are out of scope.

- [ ] **Step 2: Run static and automated verification**

Run:

```bash
git diff --check
.venv/bin/pytest -q
.venv/bin/python -m build
```

Expected: no whitespace errors, zero test failures, and wheel/sdist build succeeds.

- [ ] **Step 3: Verify against the powered pedal with USB disconnected**

Run configuration, `presets`, and `status). Save a new safety backup. Recall preset 9, recall one neighboring preset, return to preset 9, and verify each active slot through BLE `0x63`. Perform one reversible supported live edit, read it back, then restore the safety backup and verify the raw runtime state.

- [ ] **Step 4: Final review and commit**

Re-read every acceptance criterion in `docs/superpowers/specs/2026-09-08-ble-only-control.md`, inspect `git diff master..HEAD`, and record any hardware limitation honestly.

```bash
git add README.md docs/protocol.md
git commit -m "docs: explain Bluetooth-first control"
```
