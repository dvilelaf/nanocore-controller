# NANOCORE amplifier and impulse response slots

How the official app reads and writes the numbered amplifier (AMP, `.ead` files) and cabinet impulse response (IR, `.bin` files) slots of the pedal, read from its native library (`liblivtra.so`, Rust, arm64) with a disassembler and decompiler. The pure helpers that build and parse these frames are in `src/nanocore_controller/assets_protocol.py`. The framing (`F0 7D 4E 43 70 ... F7`, 7-bit packing, sequence, status byte) is the one in [protocol.md](protocol.md).

**Update, 2026-10-08: the read and write paths below were tried on a pedal; see the section "Verified on a pedal" at the end.** The rest was read from the code only. Every claim is marked:

- **Verified**: checked against data (factory files from the app, slot infos read earlier from a real pedal).
- **Code**: read in the decompiled library, not tested on hardware.
- **Unknown**: not resolved.

Function names such as `FUN_0046b778` are the Ghidra names; the ELF virtual address is the name minus `0x100000`. The library serves several products (Nanocore, Resonam, Mosaic, TPE-S10). Only the Nanocore branch (product code 7) is described.

## Summary

| Step | AMP | IR | Payload |
|---|---|---|---|
| storage info | `0x30` | `0x50` | empty |
| begin write | `0x31` | `0x51` | `u32 (length \| slot<<24)` |
| chunk (acknowledged) | `0x32` | `0x52` | `u32 (offset \| slot<<24)` + data |
| chunk (streamed, no reply) | `0x3a` | `0x5b` | same as the chunk |
| write cursor | `0x38` | `0x5a` | empty; answers `u32` bytes received |
| commit | `0x33` | `0x53` | `u32 (length \| slot<<24)`, `u32 crc32` |
| abort | `0x34` | `0x54` | empty |
| set name | `0x35` | `0x55` | `slot`, name bytes |
| slot info | `0x36` | `0x56` | `slot` (answer: 30 bytes, already implemented) |
| read | `0x39` | `0x59` | `slot, u16 offset, u8 length` |
| (IR only, after the name) | | `0x57` | `slot`; purpose unknown |

All integers are little endian. `length \| slot<<24` is one `u32` whose low 24 bits are the number and whose top byte is the slot, so the maximum is 16 MiB minus one. Slots are 0 to 29 for both kinds. Replies to begin, chunk, commit, abort, name and `0x57` are judged by their status only.

Statuses (Code): 1 bad payload, 2 no active write session, 3 slot out of range, 4 flash read/write failed, 5 CRC mismatch, 6 device not ready (string at rodata `0x11bc80`), `0x7e` command not implemented.

## Checksum (verified for IR, code for AMP)

The `checksum` of the slot info is the ordinary **CRC-32** (zlib, polynomial `0xEDB88320`, init and final XOR `0xFFFFFFFF`) of all `size` bytes of the slot.

- **Verified**: 17 IR slots read from a real pedal (sizes 4096 and, for slot 15, 3764) all equal `zlib.crc32` of the factory file `ir/NN.bin` of the same slot. Example: IR slot 2 `Eng412A`, size 4096, checksum 1785848145 is `crc32(ir/02.bin)`. The test `test_factory_ir_files_match_the_checksums_of_a_real_pedal` repeats this.
- **Code**: the commit command carries the same CRC; the app computes it with the same helper (`FUN_00580e6c` / `FUN_00580eb0`) in the IR writer (`FUN_00470740`) and in the AMP writer (`FUN_0046b778`). After a read (`FUN_00467088`) it recomputes the CRC over the bytes it got and compares it with the slot info (`READ CRC mismatch`). After a write it reads the slot info and compares the size and the checksum with what it sent (`FUN_00469cc4`).
- **Not verifiable with the factory `.ead` files**: for AMP slot 12 `MesR2` (size 12242, checksum 1107111727 = `0x41FD2F2F`, raw bytes `2f 2f fd 41`) no CRC-32 over any part of `amp/12.ead` (12302 bytes) matches; none of the 20 known amp checksums matches `crc32` of the file, of the file without its 28-byte header, of the file without all 60 bytes of framing, or of the payload alone (15 slices tried). The reason is in the next section: the `.ead` file is not what the pedal stores.

## Reading a slot (code; the framing of the read is shared by AMP and IR)

Function `FUN_00467088(slot, info_cmd, data_cmd, ...)`, used with `0x36`/`0x39` (AMP, `FUN_0046ebc8`) and `0x56`/`0x59` (IR, `FUN_00467d0c`).

1. Slot info: send `info_cmd [slot]` (reply timeout 1.2 s), parse the 30-byte record. The slot must be present, `0 < size < 65536`.
2. Repeat from `offset = 0`: send `data_cmd` with the 4 bytes `slot, offset_lo, offset_hi, n` where `n = min(200, size - offset)`; timeout 1 s. The reply payload is `slot (u8), offset (u16), total (u16), data...`. The app requires the same slot, the same offset, `total == size` and `offset + len(data) <= total`, appends the data and continues from `offset + len(data)`. An empty data part ends the loop. The loop ends when `offset >= size`.
3. Check that the length is `size` and `crc32(data) == checksum`.

Slot info record (30 bytes, implemented in `parse_asset_slot`): slot, present, active, `u32 size`, `u32 checksum`, 3 bytes (the app substitutes `0x1a1a` when they are `ff ff ff`, which is why every sample shows format id 6682), 16-byte name. The name ends at the first `00` or `ff` byte (code of `FUN_00466cfc`).

What the AMP read returns (code): the app accepts it only when it starts with `SAPF`, byte 4 is 1 and it passes a validation (`FUN_00485640`) that checks the framing and a 32-byte authentication code at the end (see the next section). So the bytes a pedal returns for an amplifier are a complete `SAPF` blob of exactly `size` bytes. This was not captured from a pedal here.

## Data formats

### IR (`.bin`), verified on the 30 factory files

Raw little endian `float32` mono samples, no header, no footer. 1024 samples (4096 bytes) for 28 of the 30 factory files; slot 11 has 695 samples (2780 bytes) and slot 15 has 941 (3764 bytes), so shorter IRs are fine. Values are not normalised (peaks from 0.38 to 1.93); the peak is within the first 12 samples in 28 of the 30 files (slots 25 and 27 peak later). The app checks only: the length is a multiple of 4, not larger than the limit reported by `0x50` (default 4096), and every value is finite (`FUN_00470740`). The sample rate is not stored. Nothing in the native library converts a `.wav` file to this format (resampling, trimming and gain are not done there; presumably the Dart side does it, which was not analysed): **Unknown** which rate (48 kHz is likely) and which normalisation the pedal expects. `ir_from_samples` and `validate_ir` do no conversion.

### AMP (`.ead`), partly understood

All 38 factory files are 12302 bytes and have this framing:

| Offset | Size | Meaning |
|---|---|---|
| 0 | 4 | magic `SAPF` |
| 4 | 4 | version, 1 |
| 8 | 16 | random-looking bytes, different in every file (a nonce) |
| 24 | 4 | payload size, 12242 in every factory file |
| 28 | 12242 | payload, entropy 7.98 bits per byte: encrypted |
| 12270 | 32 | authentication code |

The total length must equal `payload size + 60` (`0x3c`); the app checks exactly this. A file may also be wrapped in an outer `EADL` container (magic, `u16` version 1, `u32` header length at offset 6, then the header and the `SAPF` blob; `FUN_0048557c`). No sample of an `EADL` file was available, so `unwrap_eadl` follows the code only.

**What the pedal receives is not the file.** The app first verifies the authentication code (a keyed MAC with a key it derives with the labels `"mac"` and `"enc"`) and decrypts the payload (`FUN_0046b3e0`). The result is again a blob that must start with `SAPF`, version 1, with the same length rule, and that is what the AMP writer sends. Its total length is the `size` of the slot info: the 12242 bytes of the factory amps are the decrypted payload of the 12302-byte file. This explains why no checksum matches the file. The decrypted inner blob contains another 60 bytes of `SAPF` framing, i.e. 12182 bytes of model data, which I did not look at.

I did not extract the secret or implement the decryption. The consequences, which the project owner should decide on:

- Restoring an amplifier **read from the pedal** (a backup) needs no key: the blob is sent back as it came, with its own CRC-32. This is the safe use of the write commands.
- Installing a factory `.ead` file from the app, or a model from the vendor, needs the app's key material and decryption, which is what the vendor uses to protect those files. `assets_protocol.py` only validates the framing (`parse_sapf`, `validate_amp`), it does not check the authentication code and cannot tell an outer file (12302 bytes) from an inner blob (12242 bytes) except by size.
- Resonam (`NA2L`, 7516-byte NAM models) is another product and was ignored.

## Writing a slot (code)

Two paths exist, chosen at run time. Both end with the same commit. The names in the app's messages are `NANOCORE_AMP_WRITE_CHUNK`, `NANOCORE_AMP_STREAM_CHUNK`, `NANOCORE_AMP_STREAM_WINDOW`, `NANOCORE_AMP_STREAM_DELAY_MS` (environment variables with these defaults).

### 0. Preparation

- Unwrap `EADL`, verify and decrypt (AMP files), check the framing; for IR check the floats.
- Info: send `0x30` / `0x50` with an empty payload (timeout 2 s). The answer has at least 20 bytes: bytes 0 to 7 unknown, `u32` slot count at 8, `u32` size limit at 12, `u16` active slot at 16 (`0xffff` none). The Nanocore branch rejects a reply where the count or the limit is zero. For IR the app refuses a slot `>= count` and data larger than the limit; the defaults when the call fails are 30 and 4096. For AMP it refuses a slot `>= count` and a payload size above the limit; what the limit is for AMP (12242? larger?) is unknown.
- Retries: the info call is tried up to 8 times with sleeps of 200, 300, ... ms.
- The CRC-32 is computed over the whole data to be sent.

### 1. Begin

`0x31` / `0x51` with `u32 (length | slot<<24)`, `length` the number of bytes that will be sent (IR: byte count of the floats; AMP: length of the decrypted blob). Timeouts: AMP 10 s, IR 2 s. Retried on transport errors only (AMP 3 times, IR 4 times with 60 ms between). A device status other than 0 is returned at once, without a retry.

### 2a. Acknowledged chunks

`0x32` / `0x52` with `u32 (offset | slot<<24)` followed by the data. Reply timeout AMP 1.5 s, IR 0.5 s.

- IR: 64 bytes per chunk (checked in the disassembly: `add x24, x28, #0x40`), 5 attempts per chunk with 40 ms between them, 3 ms pause between chunks. After the fifth failure the app sends `0x54` (abort, timeout 0.3 s) and gives up.
- AMP: 128 bytes per chunk by default (`NANOCORE_AMP_WRITE_CHUNK`). On a timeout, "not ready" or similar error text the app asks the cursor (`0x38`), compares it with the offset it believes (`AMP write recovery cursor mismatch at offset`, `AMP write recovery SYNC failed`), waits 12 ms, resends, and halves the chunk size down to 16 bytes (steps 128, 64, 32, 16). `0x34` aborts after a failure.

### 2b. Streamed chunks (preferred when the cursor command answers)

The app first probes `0x38` (AMP) or `0x5a` (IR) once; the pedal answers with a `u32`. If the pedal answers `0x7e` the app uses the acknowledged path (2a). Otherwise (streaming path):

- Chunks go out with `0x3a` / `0x5b`, which the pedal does not answer individually: payload as in 2a. Default 48 bytes per chunk, at most 3 chunks (144 bytes) in flight, 9 ms pause after each chunk. The environment variables `NANOCORE_AMP_STREAM_CHUNK`, `NANOCORE_AMP_STREAM_WINDOW` (chunks) and `NANOCORE_AMP_STREAM_DELAY_MS` change these for AMP.
- After each burst the app asks the cursor (`0x38` / `0x5a`, timeout 1.2 s). If it advanced, the app continues from the new cursor. If it did not advance twice in a row, the app rewinds to the cursor. For AMP it also halves the chunk (not below 48 bytes), adds 6 ms to the delay and sleeps 150 ms.
- Overall limit: AMP 90 s (`AMP streamed write stalled at ... bytes`), IR 60 s. A send error whose text contains "not implemented" ends the streaming path (IR; the AMP code has the same test and then aborts and retries the whole upload, at most 3 times).

### 3. Commit

`0x33` / `0x53` with `u32 (length | slot<<24)` then `u32 crc32`. Timeouts: AMP 12 s (the pedal writes to flash), IR 2 s. Status 5 presumably means that the CRC the pedal computed differs. The app also has a `commit rejected` message whose trigger was not traced. A transport failure of the AMP commit is retried (loop in `FUN_004690e4`, count not read exactly); the abort command follows a failed session.

### 4. Name

`0x35` / `0x55` with `slot` followed by the name bytes. The name is printable ASCII (`0x20` to `0x7e`); other characters become spaces, leading ones are dropped, trailing whitespace is trimmed and at most 15 characters are kept (the record holds 16 bytes). Timeout 2 s for this product. AMP retries up to 6 times with 60, 100, 140, 180, 220 and 260 ms; IR 4 times with 50 ms. If it fails the app reports `data was committed, but setting its name failed`. Without a name the step is skipped.

### 5. After the name

- IR only: `0x57 [slot]` (timeout 0.8 s, up to 4 tries with 50 ms). **Unknown purpose**: it sits between the name and the read-back, so it is plausibly an "apply / reload" step, but it is not proven. `write_sequence` includes it for IR by default because the app always sends it; pass `apply=False` to leave it out.
- Read-back: the app reads the IR slot back (`0x56`, `0x59`) and compares size, CRC and bytes (`FUN_0046fe10`; failure message `exact readback failed`). For non-`SAPF` AMP data it reads the slot info and compares size and checksum (`FUN_00469cc4`).
- Selecting: the app then makes the slot active (`data was committed, but selecting it failed`). The selection commands are not in the functions analysed; the existing live fields `06 slot` (AMP) and `07 slot` (IR) of command `0x6d` select a slot and were verified on hardware earlier.

### Not sent by the library

`0x37`, `0x3b`, `0x58` and the product-specific commands were not found in the Nanocore branch.

## What is verified, what is code, what is unknown

| Item | Status |
|---|---|
| IR checksum is zlib CRC-32 over the whole slot; IR slot data is the raw `.bin` | **Verified** (17 of 17 slots, real pedal vs factory files) |
| IR files: float32, finite, 695 to 1024 samples | **Verified** (30 factory files) |
| Frame layout of begin, chunk, commit, name, read, cursor, abort | **Code** (several call sites each; same construction for AMP and IR) |
| Chunk sizes (IR 64, AMP 128, stream 48, window 3, delay 9 ms), timeouts and retry counts | **Code** |
| Read: 200-byte windows, reply `slot, offset, total, data`, CRC check | **Code** |
| AMP checksum is CRC-32 of the stored blob | **Code**, strongly implied by the shared helper and the read check; not verifiable with the factory files (encrypted outer layer) |
| `.ead` framing (`SAPF`, size at 24, total = size + 60, MAC at the end, encrypted payload) | **Verified** on all 38 files for the framing; the encryption is shown by the entropy and the code |
| The pedal stores the decrypted inner `SAPF` blob of `size` bytes | **Code** (inferred from the write path and from 12242 = 12302 - 60); not read from a pedal |
| `EADL` outer container | **Code**, no sample |
| `0x30` / `0x50` reply fields, meaning of the first 8 bytes | **Code** for bytes 8 to 17; **Unknown** for 0 to 7 and for the AMP size limit |
| `0x57` | **Unknown** |
| IR sample rate, `.wav` conversion, normalisation | **Unknown** (not in the native library) |
| Maximum name length 15 | **Code** (the pedal may accept 16) |
| Behaviour with a slot that is currently active, with a power loss during a write, or with a failed abort | **Unknown** |

## Open questions

1. What does `0x57` do, and is it needed for the IR to take effect?
2. Does the pedal return the decrypted `SAPF` blob for `0x39` and is its length exactly the slot-info `size`? Is `crc32` of those bytes the slot-info checksum (as the code implies)?
3. What are the first eight bytes and the limit of `0x30`? Does `0x50` really answer `30, 4096`?
4. Is a slot that is in use as the active amp or IR writable, and what happens to the sound during the write?
5. What happens to a half-written slot after a failed or aborted session: is the old content kept (staging) or lost?
6. Do the streamed commands (`0x3a`, `0x5b`) work over USB MIDI as over Bluetooth? Every function of the library first checks a flag whose error text is `Nanocore BLE MIDI not connected`, so all of this was designed for Bluetooth. Do the 48-byte chunks and the 9 ms delay hold up through USB?
7. Which sample rate and level does the pedal expect for an IR, and is a shorter IR padded or played as is?
8. May a name have 16 characters, and are non-ASCII characters shown?
9. Is the `size` of an empty slot 0 and `present` 0, so that "unused slot" can be detected before writing?

## Proposed hardware test plan (for the owner of the pedal; not run)

Stop the phone app first (one connection at a time). Back up the pedal before any write (`nanocore backup`, plus a note of the infos below). Steps 1 to 4 only read.

1. **Storage info.** Send `0x30` and `0x50` with an empty payload and save the raw replies. Expect at least 20 bytes. Compare: slot count 30, IR limit 4096, active slot equal to the active amp and IR of the runtime snapshot. This answers question 3.
2. **Slot infos of all 60 slots** (`0x36 [0..29]`, `0x56 [0..29]`) and keep the raw 30 bytes. Find the empty slots (answers question 9).
3. **Read IR slot 2.** Send `0x59 [02 00 00 c8]`, `[02 c8 00 c8]`, ... in windows of 200 until 4096 bytes; check the reply framing and that `crc32` equals 1785848145 (`0x6a71e151`) and the bytes equal `ir/02.bin` of the app. Then read an IR with 3764 bytes (slot 15) to see the shorter length.
4. **Read AMP slot 12** the same way with `0x39`, `size` 12242. Check that the data starts with `SAPF 01 00 00 00`, that bytes 24 to 27 equal `12182` (or whatever `size - 60` is) and that `crc32` equals 1107111727 (`0x41fd2f2f`). Save the 12242 bytes: this is a backup that can be restored without any key. This answers question 2.
5. **Cursor probe.** With no session open, send `0x38` and `0x5a`: expect either a `u32` or status 2/`0x7e`. (The app probes them during a session; the answer outside a session is unknown, so this is a read-only curiosity.)
6. **Write test on an unused IR slot** (pick one from step 2; preferably a slot no preset uses). Use a short test IR (for example 256 samples: an impulse at sample 0 of 0.5 and a decay). Sequence: `0x51` begin, 64-byte `0x52` chunks one by one waiting for each reply, `0x53` commit with the CRC, `0x55` name `T-IR`, then read the info and the data back with `0x56` / `0x59` and compare. Leave `0x57` out in the first run and note whether the slot already reads back correctly; try it in a second run on the same slot. Then restore: write back what was there (an empty slot may have no content to restore; note the info before).
7. **Write test on an unused AMP slot** with the blob read in step 4 from another slot (so no key is needed): `0x31` begin with its length, 128-byte `0x32` chunks, `0x33` commit with `crc32` of the blob, `0x35` name, then read back with `0x39` and compare. Check that the slot sounds like the source slot when selected with live field `06`. Watch for status 6 (not ready) and 4 (flash).
8. **Failure handling**: begin a write and send `0x34` abort; read the slot info afterwards to see whether the old content survived (question 5). Only on an unused slot.
9. **Streaming** (last, optional): repeat 6 with `0x5b` chunks of 48 bytes, 9 ms apart, polling `0x5a`, over Bluetooth and over USB.

Abort the test and restore from the backup at the first unexpected status. Do not write a slot that a preset you care about uses until steps 6 and 7 have worked on spare slots.


## Verified on a pedal (2026-10-08)

Everything here ran on a real NANOCORE over USB, with all slots backed up first.

- **Storage size.** The amplifier storage has **38 slots** (0 to 37), not 30: slots 30 to 37 hold the drive and fuzz models (`scream`, `Klone`, `OCD`, `DS2`, `PiFUZZ v.2`, `Range`, `AC`, `Fiman`). The IR storage has 30 (0 to 29). Asking for a slot beyond the count answers status 3. Reply of `0x30`: `00 20 07 00 | 00 10 00 00 | 26 00 00 00 | 00 30 00 00 | 0c 00 00 00`, that is slot count `0x26` = 38 at byte 8, size limit `0x3000` = 12288 at byte 12, active slot 12 at byte 16. For the IR (`0x50`): count 30, limit 4096, active slot 2.
- **Every slot was occupied** with factory content. The 30 IR slots read back **identical to the factory `ir/NN.bin` files** of the app; the 38 amplifier slots are all 12242 bytes.
- **Read (`0x39` / `0x59`)** works as described: windows of up to 200 bytes, the reply repeats slot and offset and gives the total. The CRC-32 of the data equals the `checksum` of the slot info for **both** IR and amplifier slots (so the amplifier checksum is the zlib CRC-32 of the stored blob). A full backup of the 68 slots took 6.7 s.
- **What an amplifier slot holds.** The pedal stores and returns the blob **in the clear**, starting with the magic `DDPB` (version 3, then a sample rate field `80 bb 00 00` = 48000). The `.ead` files of the app (and of the public tone catalog) are the encrypted `SAPF` form of the same models. Reading and writing slots of the pedal therefore needs no key; installing a `.ead` file would need the app's key, which this project does not extract or use.
- **Write (`0x31`-`0x35`, `0x51`-`0x55`, `0x57`)**: a same-content write of IR slot 29 (0.6 s) and amplifier slot 29 (1.0 s), with acknowledged chunks of 64 and 128 bytes, and the name, was accepted and the slot read back identical. `nanocore assets write` keeps the previous content in a new file first, aborts the session on any error and reads the slot back.
- **Side effect:** after a write the pedal makes the written slot the **active** one for the preset in use (amp 29 and IR 29 after the two writes above). That is live state, not stored: reloading the preset restores its own amp and IR. `write_asset` selects the previously active slot again (live fields `06` / `07`, polled until the storage info agrees) unless `keep_selection=False`; checked on the pedal: after writing slot 29 of both storages the preset in use still had amp 12 and IR 2.
- **IR from a WAV:** a synthetic WAV (44.1 kHz, 16 bit, 2400 samples) was converted (mono, 48 kHz, 1024 samples, peak 1.0), written to IR slot 29, read back identical to what was sent, and the factory content was then restored from the backup and compared identical. Whether 48 kHz and a peak of 1.0 are what the pedal expects is not verified (no factory WAV exists to compare).
- Not tried: aborting a session, streamed chunks (`0x3a`, `0x5b`), writes that change the content, writing the amplifier slots 30 to 37.
