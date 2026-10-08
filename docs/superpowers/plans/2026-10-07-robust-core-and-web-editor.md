# Robust Core and Local Web Editor Plan

> **For agentic workers:** this plan is executed by one orchestrator/reviewer and at most two parallel implementers, each in its own git worktree. Follow the file-ownership table in section 6. Every behavioural fix starts with a failing test that reproduces the defect; a reported finding that cannot be reproduced is dropped, not "fixed". Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the one-shot Bluetooth CLI into a robust library that a long-lived local server can use, then build a local web editor on it: the whole effect chain drawn at once, every edit applied to the pedal immediately, and permanent saving that happens by itself, safely, without a save button.

**Branch model:** base branch `feature/robust-core` (from `feature/ble-only-control`). Work packages land on it with `--no-ff` merges after review.

**Tech stack:** Python 3.11+, asyncio, bleak (BLE-MIDI), ALSA `amidi` (USB MIDI), unittest/pytest, ruff, mypy; later aiohttp for the server and the vendored React/Vite/TypeScript editor.

---

## 1. Why: review findings

Two read-only reviews were run. Their line numbers were unreliable (several cited lines past the end of the file), so findings are split by evidence.

### 1.1 Verified by the orchestrator reading the code

| ID | Finding | Where (verified) |
|---|---|---|
| V1 | `save` sends the flash-write command `0x46` first and only afterwards checks that the preset is still the active one. In the CLI, `status()` and the save are separated by a backup write with no re-check of the active slot. A footswitch or knob press in that window persists the live state into the wrong slot. | `controller.py:105-122`, `cli.py:233-247` |
| V2 | Safety backups are written straight to the final path with `open("x")`: no temp file, no `fsync`, default permissions, and the file is never reloaded or validated before the flash write. | `cli.py:221-227, 238-241, 256-259` |
| V3 | The safety backup holds the *live* state, which is what is about to be saved. It does not preserve the previously stored content of the slot, so it cannot undo a bad save. | `cli.py:233-241` |
| V4 | `except BaseException: pass` swallows cancellation and keyboard interrupts during cleanup. | `controller.py:46-61` |
| V5 | Preset-number validation is duplicated verbatim. | `controller.py:79-84, 107-112`, `cli.py` |
| V6 | The default adapter `hci1` is hard-coded in four places. On the development PC it no longer exists (`hci0`), which broke Bluetooth control. | `cli.py:197`, `controller.py:31`, `ble_midi.py:169`, `ble_state.py:19` |
| V7 | A device error is detected by searching the message text for `"status 0x03"`; `query` turns the device status into a plain `RuntimeError` string. | `ble_state.py:83`, `ble_midi.py:249-252` |
| V8 | Capture lists `raw_packets` and `events` grow without bound; `close()` never calls `stop_notify`; `ResponseAssembler.feed` is called outside the `try` that handles `decode_response` errors. | `ble_midi.py:176-177, 209-216, 246, 255-258` |
| V9 | USB is write-only: `MidiTransport` has only `send`, and the whole read path is typed against `BleMidiSession`. Reads over USB were verified to work with the same SysEx frames. | `transport.py:8`, `ble_state.py`, `ble_restore.py`, `controller.py` |
| V10 | `_find_active_asset` scans up to 64 slots with a 1 s timeout per silent slot, so `status()` can take about a minute. | `ble_state.py:66-90` |
| V11 | Save, restore and backup orchestration lives inside `cli._run_bluetooth`, so a server cannot reuse it. | `cli.py:207-271` |

### 1.2 Reported by the reviewers, to be reproduced by a failing test first

| ID | Claim | Test to write first |
|---|---|---|
| T1 | `parse_runtime_snapshot` accepts NaN, inf and out-of-range floats, and a `chain_order` that is not a permutation. | Feed crafted payloads. |
| T2 | `validate_backup` and `chain_order_payload` raise `TypeError` on non-integer chain entries; the CLI then dies with a traceback. | Tampered backup with `"x"` in the chain. |
| T3 | `unpack_7bit` accepts bytes with the high bit set and silently keeps a truncated final group. | `7f 01 02`, `ff`. |
| T4 | An exception inside the BLE notification handler leaves the SysEx decoder mid-message and the next packet is glued onto it; a realtime byte (`0xF8`) inside a SysEx is rejected though legal. | `F0 7D F8 ...` then `01 02 F7`. |
| T5 | Live payload builders accept strings and booleans and unbounded indices (`live_param_payload(255, 255, "0.5")`). | Direct calls. |
| T6 | `restore --apply` can leave the device half-modified on a mid-way failure and does not report partial application; a cancelled task aborts it midway. | Fake session that fails at operation N. |
| T7 | `json.loads` of a backup accepts `NaN`; no size cap. | Tampered file. |
| T8 | `read_active_state_from_session` does several non-atomic queries; a preset change in between mixes slots. | Fake session that switches slot between queries. |
| T9 | Subprocess calls have no timeout; `gatttool` capture can leave an orphan; `ble-capture` takes an address and adapter without validation. | Fake runner. |
| T10 | No `logging` anywhere; no audit trail of flash writes. | Assert log records. |

### 1.3 Kept as is (genuinely good)

Pure codecs (`BleMidiDecoder`, 7-bit packing, `encode_request`/`decode_response`/`ResponseAssembler`), frozen dataclass parsers, dependency injection through `session_factory`/`sleep`/`output`, the single-lock controller, the validate-plan-apply-read-back restore design, lazy `bleak` import, library modules that never print or exit, and the 157-test suite that runs offline in 1.5 s.

---

## 2. Target architecture

```text
src/nanocore_controller/
  errors.py            NEW  typed errors (compatible with today's ValueError/RuntimeError)
  nanocore_protocol.py      strict codecs and payload builders          [WP-A]
  backup_io.py         NEW  atomic backup write, strict backup load      [WP-A]
  ble_restore.py            validate_backup hardened                   [WP-A], orchestration [WP-C]
  session.py           NEW  SysexSession protocol + FakeSession          [Phase 0]
  ble_midi.py               BleMidiSession hardened, events()          [WP-B]
  usb_midi.py          NEW  AmidiSession: reads and writes over USB     [WP-B]
  transport.py              write-only transports kept for --dry-run   [WP-B]
  discovery.py              non-blocking, with timeouts                [WP-B]
  device.py            NEW  NanocoreDevice, transport-agnostic         [WP-C]
  controller.py             thin alias BluetoothController -> device   [WP-C]
  ble_state.py              read helpers retyped to SysexSession       [WP-C]
  cli.py                    argparse + printing only, dispatch table   [WP-C]
  baselines.py         NEW  per-slot baseline history (section 4)      [WP-C]
  serve.py             NEW  localhost HTTP/WebSocket adapter           [Phase 3]
web/                   NEW  vendored editor with chain board           [Phase 4]
```

Module renames are deliberately avoided in this plan to keep merges simple. `ble_state.py` and `ble_restore.py` keep their names; they depend only on `SysexSession`.

---

## 3. Interface contracts

These are fixed in Phase 0 so the packages can proceed in parallel.

### 3.1 `errors.py`

Every new exception subclasses the exception the code raises today, so existing tests and callers keep working.

```python
class NanocoreError(Exception): ...
class ValidationError(NanocoreError, ValueError): ...      # bad input from the user or a file
class ProtocolError(NanocoreError, ValueError): ...        # malformed device response
class DeviceStatusError(NanocoreError, RuntimeError):      # command, status, payload
class DeviceTimeout(NanocoreError, TimeoutError):          # command, maybe_applied: bool
class DeviceDisconnected(NanocoreError, RuntimeError): ...
class DeviceNotFound(NanocoreError, RuntimeError): ...
class VerificationError(NanocoreError, RuntimeError): ...  # read-back mismatch
class SlotChangedError(VerificationError): ...             # active slot moved under us
class PartialApplyError(VerificationError):                # applied, total, backup_path
class BackupError(NanocoreError, RuntimeError): ...        # unreadable, unwritable, invalid
```

`DeviceTimeout.maybe_applied` is `True` for any write: after a timeout the outcome is unknown and callers must read back.

### 3.2 `session.py`

```python
class SysexSession(Protocol):
    @property
    def connected(self) -> bool: ...
    async def connect(self) -> None: ...                  # idempotent
    async def close(self) -> None: ...                    # idempotent, never raises on a dead link
    async def send(self, message: bytes) -> None: ...     # raw MIDI message
    async def query(self, command: int, payload: bytes = b"", *,
                    sequence: int | None = None, timeout: float = 5.0) -> NanocoreResponse: ...
    def events(self) -> AsyncIterator[MidiEvent]: ...     # device-originated notifications
```

`query` raises `DeviceStatusError` for a non-zero status, `DeviceTimeout` on timeout and `DeviceDisconnected` if the link drops. `FakeSession` (scriptable responses, injectable failures) lives in the same module and replaces the ad-hoc fakes in the tests.

### 3.3 `backup_io.py`

```python
def write_backup(path: Path, document: Mapping[str, Any]) -> None
def load_backup(path: Path, *, max_bytes: int = 1_000_000) -> dict[str, Any]
```

`write_backup`: serialise with `allow_nan=False`; write to a temporary file in the same directory created with mode `0600`; `fsync`; publish with `os.link` (fails if the destination exists, no replace); `fsync` the directory; remove the temp file. Refuses an existing path or a symlink. Raises `BackupError`.
`load_backup`: size cap, strict JSON (rejects `NaN`/`Infinity`), then `validate_backup`. Raises `BackupError` or `ValidationError`.

### 3.4 `NanocoreDevice` (WP-C)

One object owns one `SysexSession` and one `asyncio.Lock`; every operation holds the lock. Constructed inside a running loop.

| Method | Behaviour |
|---|---|
| `read_live()` | One `0x63` query. Fast path for the UI. Returns the typed snapshot. |
| `read_state()` | Full state. Consistent: re-read `0x63` at the end, retry once if it differs. Active amp/IR slot probed from a cache first, scanned only on mismatch, with short timeouts. |
| `catalog()` | Preset names. |
| `select_preset(n)` | Recall with read-back verification. |
| `set_param`, `set_enabled`, `set_variant`, `set_volume`, `set_chain_order`, `select_amp`, `select_ir` | Strictly validated typed setters using the SysEx live fields. Return after the device ack, no full state read. |
| `backup_active(path)` | Consistent read, atomic write, reload and validate. |
| `save_active(backup_path)` | See section 4.2. |
| `restore_active(document, safety_backup_path)` | See section 4.3. |
| `events()` | Async iterator of device-originated changes (knob and preset changes on the pedal). |

All writes log to the `nanocore.audit` logger: command, sequence, slot, status and the result of every verification.

---

## 4. Safety design

### 4.1 Live edits versus permanent save

The protocol keeps them apart: live fields (`0x6d`) change only the working state; `0x46` writes the slot to flash. The editor applies live changes immediately and saves permanently on its own, but not on every change:

- **Immediate live apply.** Every control change is sent as soon as it happens, coalesced per parameter (only the latest value of a dragged knob is sent, at most about 30 per second).
- **Debounced autosave.** A save is scheduled after the user has been idle for a configurable time (default 3 s), only if the state is dirty, and never more than once every 10 s, to limit flash wear.
- **Baseline backup.** The first time a slot is edited in a session, the state of that slot is stored before the edit, while the live state still equals the stored one. That is the file that can undo an unwanted autosave. History is kept per slot (default last 20 entries) under `$XDG_DATA_HOME/nanocore-controller/baselines/slot-NNN/`. Limit: if the user turned a knob on the pedal before connecting, the live state is already dirty; this is detected where possible and reported in the UI.
- **Kill switch.** `nanocore serve --no-autosave` keeps live apply only.
- **UI states.** Saved, unsaved changes, saving, error. Never a button.

### 4.2 `save_active`

Under the lock, in this order, aborting on any failure and never reaching the write step with a stale view:

1. Read `0x63`; remember slot S.
2. Read the full state and write it atomically to `backup_path`; reload it and run `validate_backup`.
3. Read `0x63` again. If the active slot is not S, raise `SlotChangedError` and write nothing.
4. Send `0x46 S`.
5. Read back and verify the slot and the saved parameters match; raise `VerificationError` otherwise.

The slot argument disappears from the public API: callers can only save the active slot, and the check precedes the write.

### 4.3 `restore_active`

Validate and plan, then write the safety backup (atomic, reloaded, validated), check the active slot matches the document, apply under `asyncio.shield` so a disconnecting client cannot cancel it midway, read back and verify. On a failure after the first write raise `PartialApplyError(applied, total, backup_path)` and attempt a best-effort restore of the pre-state.

---

## 5. Phases and work packages

### Phase 0: foundations (orchestrator)

- [x] Create `feature/robust-core` and the two worktrees (section 6).
- [x] Add `[tool.ruff]` and `[tool.mypy]` to `pyproject.toml`, add `ruff` and `mypy` to the `dev` extra, add `py.typed`, move `bleak` into the core dependencies (Bluetooth is the default transport), add the `serve` extra placeholder. Fix the existing mypy errors and the lint noise that is not a behaviour change.
- [x] Write `errors.py` and `session.py` as in section 3. `session.py` also exports `READ_ONLY_COMMANDS`, the set of commands whose timeout does not leave the device in an unknown state.
- [x] Remove the hard-coded `hci1` default (12 places): the default is `hci0` (`DEFAULT_ADAPTER`), the CLI detects the lowest-numbered adapter in `/sys/class/bluetooth` when none is given, and `config.validate_adapter_name` / `validate_bluetooth_address` accept only well-formed values so they can never be taken for an option by `gatttool`.
- [x] Commit. Both worktrees are created from this commit.

Acceptance: all existing tests green (183), `ruff` clean, `mypy` clean, contracts committed.

### Phase 1: parallel

**WP-A (implementer 1, worktree `wp-a`): strict protocol and safe backups.**
Owns `nanocore_protocol.py`, `backup_io.py`, `validate_backup` and `_integer` in `ble_restore.py`, and their tests.
Fix T1, T2, T3, T5, T7 and V2/V3's file half: strict finite/range/permutation checks at parse time; `type(x) is int` guards before `sorted`; `unpack_7bit` rejects truncated groups and bytes `>= 0x80`; strict payload builders with exported range constants; `backup_io` as in 3.3; command IDs and sizes become named constants in `nanocore_protocol.py`; one `validate_display_number`. Raise the typed errors of section 3.1.
Acceptance: each of T1-T3, T5, T7 has a test that failed before the fix; fuzz-style tests for `unpack_7bit`, `ResponseAssembler` and `validate_backup` (random and malformed input never raises anything but `NanocoreError`).

**WP-B (implementer 2, worktree `wp-b`): session layer.**
Owns `ble_midi.py`, `usb_midi.py`, `transport.py`, `discovery.py` and their tests.
Fix V7, V8, V9, T4, T9: `BleMidiSession` implements `SysexSession` (idempotent `connect` and `close` with `try/finally`, `stop_notify`, timeouts around send and close, disconnect callback that fails pending queries with `DeviceDisconnected`, decoder reset on error, realtime bytes tolerated, capture lists opt-in and bounded, stale events drained before each query, `events()` fan-out, typed `DeviceStatusError`); new `AmidiSession` for USB with a long-lived `amidi -d` receiver and the shared decoder; subprocess timeouts, kill fallback and stderr kept in errors; address and adapter validation; blocking calls moved off the event loop.
Acceptance: tests for each defect using `FakeSession` and a fake subprocess; no real BLE or ALSA access in the suite.

**Orchestrator in parallel (WP-C), against the contracts only.** Writes `device.py`, `baselines.py` and the tests for `save_active`, `restore_active`, `read_state` consistency and typed setters using `FakeSession`, so integration is a wiring exercise.

### Phase 2: integration (orchestrator)

- [ ] Review and merge WP-A, then WP-B (checklist in section 7).
- [ ] Wire `NanocoreDevice` to the real strict parsers, `backup_io` and the hardened sessions; delete the duplicated retry and validation code; move the CLI workflows into the device; turn `cli.py` into a dispatch table where "supported transports" is data; enable reads (`status`, `presets`, `backup`) over USB.
- [ ] Replace `BluetoothController` by a thin compatibility alias.
- [ ] Add the `logging` configuration to the CLI only (`-v`), never in the library.
- [ ] Update README and `docs/protocol.md`.

Acceptance: full suite, `ruff`, `mypy` green; the CLI behaves as before for every existing command; V1-V11 and T1-T10 each have a regression test.

### Phase 3: local server (implementer 1, worktree `wp-c`)

`nanocore serve [--host 127.0.0.1] [--port N] [--transport usb|bluetooth] [--no-autosave] [--read-only]`, built on `NanocoreDevice` only.

- Contract first: the orchestrator writes `docs/api.md` (REST for state, presets, edits, baselines; WebSocket for state pushes and device-originated changes) before implementation starts.
- Security: bind to `127.0.0.1`; reject requests whose `Host` or `Origin` is not the served origin; a per-run random token required on every write and on the WebSocket; JSON bodies only, with size limits; per-client rate limit; one writer queue; device strings are data, escaped by the client and length-limited by the server; `--read-only` rejects all writes.
- Behaviour: live edit coalescing, debounced autosave and baselines as in section 4.1, skipping the autosave when the active slot differs from the slot being edited; reconnect with backoff; clean shutdown that never interrupts a flash write.

Acceptance: tests with `FakeSession` including malicious Origin/Host, missing token, oversized body, concurrent edits, device drop mid-save and slot change mid-edit.

### Phase 4: web editor (implementer 2, worktree `wp-d`)

- Vendor the MIT editor from `work/livtra-nanocore-editor/app` into `web/`, with its `LICENSE` and an attribution notice.
- Add a `BridgeTransport` next to Web MIDI, Web Bluetooth and the Simulator, talking to `nanocore serve`.
- New chain board: one card per block (FX1, FX2, AMP, CAB, MOD, DEL, REV, EQ) in the real chain order, each with its switch, type selector and every parameter visible at once; drag to reorder; 4 by 2 grid on desktop, stacked on phones.
- No save and no "send patch" buttons: state indicator only (section 4.1).
- State hydrated from the device; device-originated changes pushed live.
- Built assets served by the Python server and packaged with the wheel.

Acceptance: component tests for the chain board and the autosave indicator; the Simulator path still works without a device; accessibility basics (keyboard operation, labels).

### Phase 5: hardware verification and docs (orchestrator, with the user)

Staged and only with the user's explicit go-ahead at each stage: read-only over USB and over Bluetooth; live writes on a scratch preset chosen by the user, restored from its baseline; one autosave round trip verified by recalling another preset and coming back; killing the server and the link mid-edit. Results go to `docs/HARDWARE_VERIFICATION.md`.

---

## 6. Parallel execution

| Worktree | Branch | Owner | Files it may touch |
|---|---|---|---|
| main checkout | `feature/robust-core` | orchestrator | `pyproject.toml`, `errors.py`, `session.py`, `device.py`, `baselines.py`, `controller.py`, `ble_state.py`, `cli.py`, `state.py`, `config.py`, docs |
| `../nanocore-controller-wt/wp-a` | `wp-a-protocol` | implementer 1 | `nanocore_protocol.py`, `backup_io.py`, `ble_restore.py` (only `validate_backup` and `_integer`), their tests |
| `../nanocore-controller-wt/wp-b` | `wp-b-session` | implementer 2 | `ble_midi.py`, `usb_midi.py`, `transport.py`, `discovery.py`, their tests |

Rules: an implementer never edits a file it does not own; if it needs a contract change it stops and reports. Tests run with the shared interpreter, `/media/david/DATA/repos/nanocore-controller/.venv/bin/python -m pytest`, from inside the worktree (the `pythonpath = ["src"]` setting puts that worktree's sources first). Implementers do not touch the device, do not run the CLI against hardware, and do not push. Every package ends as a clean series of commits with the tests green.

Merge order: WP-A, then WP-B, each `--no-ff` into `feature/robust-core` after review; the base is re-verified after each merge.

## 7. Review checklist (orchestrator)

1. Each defect has a regression test that fails on the pre-fix code (checked by running the new tests against the base commit).
2. Nothing outside the owned files changed; contracts unchanged.
3. Typed errors only; no new bare `ValueError`/`RuntimeError` at module boundaries; no `except BaseException`; no swallowed exceptions without a log line.
4. No blocking calls inside `async def`; every external wait has a timeout; every resource is released on failure and on cancellation.
5. No NaN, bool or string accepted where a number is expected; every write path validates ranges.
6. No flash write is reachable without the order check in section 4.2.
7. `ruff` and `mypy` clean; the suite stays offline and fast; no real BLE, ALSA or filesystem access outside temp directories.
8. Diffs are small, readable and match the surrounding style.

## 8. Risks and decisions

- **Only one BLE connection.** The server holds it. The official phone app and the web editor cannot be connected at the same time; USB has no such limit.
- **Flash wear and surprise overwrites.** Mitigated by the idle debounce, the minimum interval, the baseline history and the kill switch. This is the one place where "no save button" costs something, and the user accepted the trade.
- **Reads over USB** rely on a behaviour verified once on firmware 1.04. `AmidiSession` must fail with a clear error, not hang, if a firmware answers differently.
- **Out of scope:** firmware updates, preset import from factory files, global settings (loopback, input gain, USB/BT volume, MIDI channel), toggling the pedal's Bluetooth.

## 9. Definition of done

All findings V1-V11 and T1-T10 resolved or dropped with evidence; the CLI works unchanged over Bluetooth and over USB for every command; `nanocore serve` and the web editor work against the Simulator and against the real pedal as verified in Phase 5; the suite, `ruff` and `mypy` are green; README, `docs/protocol.md`, `docs/api.md` and `docs/HARDWARE_VERIFICATION.md` match the code.

---

## 10. Findings during implementation

### 10.1 The reviewers' line numbers were unreliable

Both review reports cited lines past the end of the files. Every finding was therefore re-verified by reading the code (section 1.1) or reproduced with a failing test (section 1.2) before any fix. New tests were also run against the base commit: WP-A's failed 197 times and WP-B's 34 times, plus the collection errors of the modules that did not exist yet. The safety tests of `NanocoreDevice` were mutation-checked (removing the pre-write slot check, the backup step or the rollback each makes a test fail).

### 10.2 The editor's model and the pedal's runtime model differ

The vendored editor stores, per block, a `typeId` and real-world parameter values, and writes through CC messages. The pedal's runtime snapshot (command `0x63`) is a list of eight entries, each with `variant` and normalized `params`. Comparing the 40 factory presets in `work/factory_presets.json` (official format, same slot order as the runtime) with the editor's catalog, by number of parameters per variant, gives:

| Runtime index | Block | Evidence |
|---|---|---|
| 0 | FX1 | variants and counts identical to the catalog |
| 1 | FX2 | matches except variant 9 (5 observed, 4 in the catalog) |
| 2 | AMP | 5 parameters |
| 3 | CAB | 2 parameters |
| 4 | DEL | catalog minus one parameter, in every variant |
| 5 | MOD | catalog minus one parameter, in every variant |
| 6 | REV | catalog minus one parameter, in every variant |
| 7 | EQ | identical to the catalog |

So the runtime order is `fx1, fx2, amp, cab, del, mod, rev, eq`, which swaps `mod` and `del` with respect to the editor's chain-order numbering (`fx1:0, fx2:1, amp:2, cab:3, mod:4, del:5, rev:6, eq:7`). Which parameter is missing from the delay, modulation and reverb arrays, how `chain_order` values relate to runtime positions, and the `variant` to `typeId` correspondence are NOT yet known.

### 10.3 Consequences for the design

- **Writes use the documented CC protocol** (hardware-verified by the editor's author) through an allow-listed `cc` edit operation, plus the SysEx operations for chain order and amp/cab slots that the editor already uses. A wrong runtime-to-editor mapping can therefore never corrupt the pedal.
- **The mapping is used only to display values read from the pedal.** Until it is calibrated, the web editor marks the blocks whose mapping is unverified.
- **Calibration (Phase 5, needs the user):** on a scratch preset, send a distinctive value for each documented CC and diff the runtime snapshot; this fixes the parameter positions, the missing parameter and the chain-order semantics. Live state only, with a baseline taken first. It will be offered as a scripted, read-back-verified procedure, not run without the user's go-ahead.
- **Tooling:** the editor needs Node 22 (`web/.nvmrc`); `nvm` already has 22.22.3.

---

## 11. Outcome

Phases 0 to 4 are implemented and merged on `feature/robust-core`; phase 5 was done for everything that does not store anything in the pedal's flash. What changed with respect to the plan:

- **Writes through the documented controllers.** As decided in 10.3, the editor writes parameters as MIDI CC through an allow-listed `cc` operation; the SysEx operations are used for the chain order and the amp and cabinet slots.
- **The mapping was measured, not inferred** (`scripts/calibrate_mapping.py`, `docs/calibration/`). It corrected the order of two blocks, found which parameters have no controller and found that reverb variants skip number 4.
- **Writes show up about 20 ms late.** The server waits for that before reading, the web editor ignores stale patches for a control the user just edited, and `docs/protocol.md` records the timing.
- **Baselines are recorded when a preset is first seen**, not at its first edit, so a change made on the pedal cannot slip into them.
- **Final review.** A fresh reviewer found a read-only server that could still store changes made on the pedal, a failed baseline restore that would have been autosaved, a recall that could discard unsaved edits, and weaker rate limits than documented. All were reproduced or read in the code, fixed, and given a test that fails without the fix (checked by mutation).
- **Removed as dead code:** the old write-only `transport.py` and `state.py`.

What is still open is listed in `docs/HARDWARE_VERIFICATION.md`: the new `save_active` and `restore_active` and the autosave have not been run against a real pedal, nor the server over Bluetooth, nor the editor in a browser. The meaning of the chain-order values is unverified.
