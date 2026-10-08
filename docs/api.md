# Local server API (v1)

Contract between `nanocore serve` and the web editor. The server is a thin adapter over `NanocoreDevice`; nothing in this document is implemented outside that class's guarantees.

Base URL: `http://127.0.0.1:<port>`. The server binds to `127.0.0.1` unless `--host` explicitly says otherwise (it then prints a warning and also accepts that host name in `Host`). Every response carries `X-Nanocore-Api: 1`.

## Security model

- **Host check.** `Host` must be `127.0.0.1:<port>` or `localhost:<port>`. Anything else is `403 forbidden`. This blocks DNS-rebinding.
- **Origin check.** If an `Origin` header is present it must equal `http://<Host>`. A page on another site can therefore never write to the pedal.
- **Token.** At start-up the server prints `http://127.0.0.1:<port>/#token=<random>` (a fresh 256-bit value per run). The page reads the token from the URL fragment, which never reaches server logs, keeps it in memory, removes it from the address bar and also keeps it in the tab's `sessionStorage` (so a reload does not drop the connection; it is private to that tab, gone when the tab closes and never put in `localStorage`), and sends it as the header `X-Nanocore-Token` on every `/api` request. The WebSocket authenticates with its first message (below). A missing or wrong token is `401 unauthorized`; comparison is constant-time.
- **`--no-token`.** Turns the token check off for people who prefer a fixed address. Only allowed on the loopback address (the server refuses to start otherwise) and it prints a warning. `Host` and `Origin` are still checked, so web pages in the browser stay blocked; what is lost is protection against other programs or users on the same computer.
- Static files and `GET /api/health` need no token and contain no state.
- **Limits.** JSON bodies and WebSocket messages are at most 64 KiB; an edit request has at most 64 operations; each client may send at most 30 parameter edits per second (`429 rate_limited` beyond that).
- **Read-only mode** (`--read-only`): every write endpoint answers `403 read_only`, and the autosave is switched off, so changes made on the pedal itself are never stored either.
- **Limits on work.** At most 16 WebSocket connections (more get `503 busy`); at most 256 operations waiting for the pedal (`503 busy`); every operation costs at least one token of the rate limit, and a request bigger than the burst uses the whole budget instead of being refused for ever; a pedal that stops answering three times in a row is treated as a dropped link.
- **Device text is data.** Preset and asset names come from the pedal. They are at most 32 characters with control characters removed by the server, and clients must render them as plain text, never as HTML.

## Errors

Every error body is `{"error": {"code": <string>, "message": <string>, "maybe_applied": <bool, optional>}}`.

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `bad_request` / `validation` | Malformed JSON, wrong content type, unknown operation, value out of range. Validation of a whole edit request happens before any write. |
| 401 | `unauthorized` | Missing or wrong token. |
| 403 | `forbidden` / `read_only` | Host or Origin rejected, or read-only mode. |
| 404 | `not_found` | Unknown route, baseline id, or model kind or slot. |
| 405 | `method_not_allowed` | |
| 409 | `slot_changed` | The active preset changed under the operation. Nothing was written. |
| 409 | `autosave_disabled` | `save-now` while started with `--no-autosave`. |
| 409 | `unsaved_changes` | A preset recall was refused because the edits of the active preset could not be stored (a failed store answers with its own error instead). Nothing was recalled. |
| 413 | `too_large` | Body over the limit. |
| 429 | `rate_limited` | Too many edits. |
| 500 | `internal` / `backup_failed` | Unexpected failure, or a backup file could not be written (for example the baseline before an edit; the edit is then not applied). Messages never contain file paths. |
| 502 | `device_status` | The pedal answered with a non-zero status. |
| 502 | `verification_failed` / `protocol` | A read-back did not match, or the pedal sent something malformed. |
| 503 | `disconnected` | No link to the pedal. The server reconnects with backoff (1 s doubling to 30 s). |
| 503 | `shutting_down` | The server is stopping. |
| 503 | `busy` | Too many connections or too many operations waiting for the pedal. |
| 504 | `device_timeout` | The pedal did not answer. `maybe_applied` tells whether a write may have taken effect. |

When an edit fails after some operations were sent, the error object also carries `"applied": <n>`, the number of operations that took effect before the failure.

## State model

```jsonc
{
  "rev": 42,                         // increments on every change of preset or live state; connection and autosave changes have their own messages and do not change it
  "connected": true,
  "transport": "bluetooth",          // or "usb"
  "read_only": false,
  "preset": {"slot": 8, "display_number": 9, "name": "FunkCln"},
  "live": {
    "volume": 84,                    // 0..100
    "chain_order": [0, 1, 2, 3, 4, 5, 6, 7],
    "effects": [                     // always 8, in runtime index order
      {"index": 0, "effect_id": 7, "enabled": true, "variant": 0, "params": [0.5, 0.47]}
    ]
  },
  "autosave": {
    "enabled": true,
    "state": "saved",                // "saved" | "dirty" | "saving" | "error"
    "last_saved_at": "2026-10-07T20:00:00+00:00",
    "error": null
  }
}
```

`params` are normalized floats in `[0, 1]`, exactly as the pedal reports them. Block names are resolved by the client from `effect_id` with the editor's own block table, which is checked against the pedal during hardware verification. Amp and cabinet slot names are not part of the live state because reading them is slow; they come from `GET /api/assets`.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /api/health` | `{"ok": true, "token_required": true, "clients": 0}`. No token. The page uses `token_required: false` to connect without one; `clients` is the number of pages connected and authenticated. |
| `GET /api/state` | The state document above. |
| `GET /api/presets` | `{"presets": [{"slot": 0, "display_number": 1, "name": "..."}]}`, read from the pedal's catalog. |
| `GET /api/assets` | `{"amp": {"slot": 12, "name": "MesR2"}, "ir": {"slot": 2, "name": "Eng412A"}}`. Slow path, cached by the server. |
| `POST /api/preset` | Body `{"display_number": 9, "discard": false}`. Recalls a preset with read-back verification and returns the new state. Refused with `409 unsaved_changes` when the active preset has unsaved edits, unless `discard` is true. |
| `POST /api/edit` | Body `{"client_seq": 17, "ops": [...], "slot": 8}`, below; `client_seq` and `slot` are optional, and with `slot` the edit is refused with `409 slot_changed` unless that preset is the active one. Returns `202` with `{"applied": n, "rev": 43}`. |
| `GET /api/baselines?slot=8` | Baseline history of a slot (default: the active one), newest first, as a bare JSON array: `[{"id": "20261007T200000Z", "captured_at": "...", "name": "..."}]`. |
| `POST /api/baselines/restore` | Body `{"slot": 8, "id": "..."}`. Loads that baseline into the live state (the autosave then stores it). |
| `POST /api/save-now` | Stores the unsaved edits of the active preset and answers `{"saved": bool, "autosave": {...}}`. The editor's save button. |
| `GET /api/settings` | The pedal's global settings (not part of any preset): `{"version": 1, "wireless_enabled": true, "loopback_enabled": false, "input_gain_db": 1, "usb_volume": 100, "bt_volume": 100, "midi_channel": 0, "volume_floor": null, "volume_ceiling": null}`. `midi_channel` 0 is omni. `volume_floor` and `volume_ceiling` are only reported by pedals with version 2 of the layout. Read inside the writer queue. |
| `POST /api/settings` | Body: a non-empty JSON object with any of `wireless_enabled` (bool), `loopback_enabled` (bool), `input_gain_db` (int -20..20), `usb_volume` (int 0..100), `bt_volume` (int 0..100), `midi_channel` (int 0..16). Anything else, a wrong type or a value out of range is `400 validation` and nothing is sent. The write is verified against the pedal's reply (`502 verification_failed` if it did not take a value) and the new settings are returned in the same shape as the `GET`. The pedal keeps these settings itself: there is no save step, nothing here marks a preset unsaved or touches the baselines. Refused with `403 read_only`; counts as one operation of the rate limit. Turning `wireless_enabled` off drops the Bluetooth link of a Bluetooth session. |
| `GET /api/preset-file` | The active preset as a JSON document in the backup format (`NanocoreDevice.read_state()`: `format`, `version`, `device`, `preset`, `assets`, `raw_snapshot`), with `Content-Disposition: attachment; filename="preset-09-Name.json"` (slot number, then the name reduced to `[A-Za-z0-9_-]`; the name part is left out when nothing is left). `preset.name` is the name the page shows, a live rename included. Allowed in read-only mode. |
| `POST /api/preset-file` | Body: such a document (`application/json`, at most 64 KiB). Applies the CONTENT of the file to the ACTIVE preset, live: the document is validated with the rules of the backup restore (`400 validation` for anything invalid, before the pedal is touched), its `preset.slot` and `display_number` are rewritten to the active slot (so a file taken from another preset can be loaded), the current state is written to a safety backup, the baseline of the stored preset is recorded as before any edit, and the document is applied and read back (`NanocoreDevice.restore_active`, which puts the previous state back if it fails). Then the preset name of the file is applied as a live rename when it is 1 to 8 printable ASCII characters (otherwise the current name is kept). The preset becomes unsaved; the user must `POST /api/save-now`. Answers `200` with the new state. Refused with `403 read_only`; counts as one operation of the rate limit. |
| `GET /api/models` | The occupied slots of both model storages, read from the pedal inside the writer queue each time (nothing is cached): `{"amp": [{"slot": 12, "name": "MesR2", "size": 12242, "crc32": 123456789, "active": true}], "ir": [...], "ead": false}`. `ead` says whether an `.ead` decryptor is configured (`--ead-decryptor` or `NANOCORE_EAD_DECRYPTOR`). The pedal has 38 amplifier slots (0 to 37; 30 to 37 are drive models) and 30 IR slots. `active` marks the slot the active preset uses. Slow (about one pedal query per slot). `503 disconnected` without a pedal. |
| `GET /api/models/{kind}/{slot}` | `kind` is `amp` or `ir`, `slot` a decimal integer; anything else is `404 not_found`, and a number past the storage is `400 validation`. Returns the data as `application/octet-stream`, read from the pedal and verified by length and CRC-32 (`502 protocol` if it does not match), with `Content-Disposition: attachment; filename="amp-12-MesR2.bin"` (kind, slot, then the name reduced to `[A-Za-z0-9_.-]`; the name part is left out when nothing is left). An IR file is raw little-endian float32 samples; an amplifier file is the opaque blob the pedal stores (it starts with `DDPB`). Allowed in read-only mode. |
| `POST /api/models/{kind}/{slot}` | Writes one slot. The body is the raw file (any content type) and the optional query `?name=` (1 to 15 printable ASCII characters, otherwise the slot keeps its name). For `ir`, a body that starts with `RIFF` is a WAV file and is converted (`ir_import.wav_to_ir`: mono, 48 kHz, at most 1024 samples, peak 1.0); any other body must be raw float32 IR data (a whole number of finite samples, at most 1024). For `amp` the body must be a blob read from a pedal (it starts with `DDPB`), or an `.ead` file (it starts with `SAPF` or `EADL`), which is installed only through the decryptor the user configures with `--ead-decryptor` / `NANOCORE_EAD_DECRYPTOR` (see `docs/ead-decryptor.md`; the project ships no key and no decryption); without a decryptor it is refused with `400 validation` and a message that says how to configure one. The old content of the slot is first written to a NEW file `<kind>-<slot>-<UTC stamp>.bin` in the `model-backups` directory (mode 0600, the directory 0700; next to the autosave backups, or `--model-backup-dir`; nothing is ever pruned or replaced). The write is verified by reading the slot back (`NanocoreDevice.write_asset`) and the slot that was active before stays selected. Answers `200` with the slot as in the list (`active` is read after the write). The body is limited to 4 MiB for this route only (`413 too_large`, also for a chunked body; every other route keeps 64 KiB). Refused with `403 read_only`; counts as one operation of the rate limit; `503 disconnected` without a pedal; `400 validation` for invalid data before the pedal is touched; device failures as in the error table. It is not an edit of the preset: nothing becomes unsaved, no baseline is taken, the autosave is untouched, and the server forgets its cached `GET /api/assets` answer (also after a failure). |
| `POST /api/revert` | Reloads the stored version of the active preset (drops the unsaved edits) and returns the new state. The editor's restore button. |

### Edit operations

All values are validated strictly (integers are integers, floats are finite, ranges are enforced) before any operation is sent.

| `op` | Fields |
|---|---|
| `param` | `effect` (0..7), `index`, `value` (0.0..1.0) |
| `enabled` | `effect`, `enabled` (boolean) |
| `variant` | `effect`, `variant`, `params` (the complete parameter list) |
| `volume` | `value` (0..100) |
| `name` | `name` (1 to 8 printable ASCII characters; trailing spaces are dropped, anything else is `400 validation`) |
| `chain_order` | `order` (a permutation of 0..7) |
| `amp` | `slot` |
| `ir` | `slot` |
| `cc` | `cc` (an allow-listed controller number), `value` (0..127), sent as a Control Change on channel 1 |

`cc` goes through `NanocoreDevice.send_midi` with the documented MIDI map. The allowed controller numbers are the block on/off and type controllers (as listed in `mappings.BLOCKS`), every controller in `mappings.PARAMETERS` and the tuner (80); any other number is `400 validation`. Raw MIDI or SysEx is never accepted. A `cc` edit marks the autosave dirty like any other, but is not echoed as a patch; the next read of the pedal reports its effect.

The server coalesces consecutive `param` operations on the same `effect` and `index` (and `cc` operations with the same controller number), within one request and across queued requests, and sends only the latest value, at most about 30 times per second per parameter. Operations are applied in order by a single writer; a failure stops the request and reports how many were applied.

A `name` edit renames the active preset in the pedal's working memory (`NanocoreDevice.set_preset_name`, verified against the pedal's echo). Like every edit it makes the preset unsaved: the stored name changes only with `POST /api/save-now` (or the autosave timer). Until then `preset.name` in the state and the entry of that slot in `GET /api/presets` show the new name, and other pages receive a `patch` with `{"op": "name", "name": "..."}`. After a successful save the names are read again from the pedal's catalog; `POST /api/revert`, a recall with `discard`, and leaving the preset on the pedal itself also read them again, so the stored name shows again.

## WebSocket `/ws`

1. The client connects and must send `{"type": "auth", "token": "<token>"}` within 3 seconds, otherwise the server closes the socket (`4401`).
2. The server answers `{"type": "state", "rev": n, "state": {...}}` with the full document.
3. Server to client afterwards:
   - `{"type": "state", "rev": n, "state": {...}}`: full document, sent after reconnects and preset changes.
   - `{"type": "patch", "rev": n, "ops": [...]}`: the same operation objects as above, for edits made by other clients or on the pedal itself.
   - `{"type": "autosave", "state": "dirty|saving|saved|error", "detail": null}`
   - `{"type": "connection", "connected": false, "reason": "..."}`
   - `{"type": "error", "error": {...}}` (for a failed client edit it also carries the `client_seq` of that edit)
4. Client to server: `{"type": "edit", "client_seq": n, "ops": [...]}` with the same semantics as `POST /api/edit`; failures come back as an `error` message.

A client that sees a gap in `rev` discards its model and asks for `GET /api/state`.

## Saving

By default saving is manual. Edits are applied live, which changes only the working memory (RAM) of the pedal; the stored preset in its flash is untouched until `POST /api/save-now`. The state reports `autosave.state` as `dirty` while there are unsaved edits (also when the change was made on the pedal itself), `saving` during a store and `saved` otherwise; `autosave.enabled` is `false` in this mode.

- `POST /api/save-now` stores the active preset (`{"saved": true|false, "autosave": {...}}`; `saved` is `false` when nothing was unsaved).
- `POST /api/revert` reloads the stored version of the active preset and answers with the full state, which is also sent to connected pages. The edits in RAM are dropped. Nothing is stored.
- `POST /api/preset` with unsaved edits in the active preset is refused with `409 unsaved_changes` unless the body has `"discard": true`; with it the edits are dropped, never stored.
- Read-only servers refuse all three (`403 read_only`), track nothing and write nothing.
- Closing the server never stores unsaved edits.

Before the first edit of a preset in a run the server records a baseline of its stored state, so that a save can be undone (`GET /api/baselines`, `POST /api/baselines/restore`). Flash writes and their safety checks are those of `NanocoreDevice.save_active`; see the plan, section 4. A failed save leaves the state `error`; nothing is retried by itself.

## Autosave semantics (opt-in)

With `--autosave` the server also stores by itself. After the user has been idle for `--autosave-delay` seconds (default 3) it stores the preset permanently, provided that:

- the active preset is still the one that was edited (otherwise the save is skipped and the state stays `dirty` with an explanatory `error`);
- at least 10 seconds have passed since the previous flash write (a flush on preset change, on shutdown or by `save-now` ignores the idle delay and this interval);
- a baseline of that slot was recorded before the first edit of the session.

A recall (`POST /api/preset`) flushes the pending save first and is refused if that save fails, so no edit is lost. If recording the baseline fails the edit is not applied. Shutting the server down also stores what is pending. `--no-autosave` is accepted and does nothing (it is the default).

The security headers on static files include a strict `Content-Security-Policy`; no CORS headers are ever sent.
