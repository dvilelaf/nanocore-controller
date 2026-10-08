# NanoCore Editor — web UI

The Vite + React + TypeScript app. In this repository it is the front end of
`nanocore serve`: the local Python server owns the connection to the pedal and the
page talks only to that server (`/api/*` and `/ws`, see [`docs/api.md`](../docs/api.md)).
The browser never sends raw MIDI or SysEx to the pedal in this mode.

## Requirements

Node 22 (`.nvmrc`; the project is checked with 22.22.3).

```bash
nvm use                 # picks the version from .nvmrc
npm ci
npm test                # vitest, jsdom, fake WebSocket and fetch; no hardware or network
npm run lint            # oxlint
npm run build           # production build -> dist/ (relative asset URLs)
```

## Running it against the server

```bash
npm run build
nanocore serve --static-dir web/dist
```

The server prints an address like `http://127.0.0.1:<port>/#token=...`. Open exactly that
address. The page reads the token from the fragment, keeps it in memory only, and removes it
from the address bar. Reloading the page therefore loses the token: open the printed address
again.

The editor has one way to reach the pedal: the server. A page that is not served by
`nanocore serve` (for example `npm run dev`) says so and connects to nothing. The original editor's
Simulator, Web MIDI and Web Bluetooth transports were removed: they never reached the features of
this project (presets, manual saving, models, settings), and were not verified on a pedal. The tests
use a fake transport (`src/test/fakeTransport.ts`, registered as `'test'` in `src/setupTests.ts`).
To exercise the real thing while developing, build and serve through `nanocore serve`.

## What the screen shows

- One screen, no tabs. Effect chain: one card per block in the pedal's chain order, each with power,
  type and every parameter the pedal has as a slider (the editor's own list has a few more, like a Level on MOD, DEL and REV, that the pedal does not have; they are hidden when connected). Reorder by dragging the handle.
- Preset row: a dropdown with the presets stored on the pedal (names come from the pedal and are shown
  as plain text), a save button, a restore button and an "Unsaved changes" badge. Edits stay in the
  pedal's working memory until you save; restoring reloads the stored version; switching preset with
  unsaved changes asks first.
  The same row has icon buttons to rename the preset (a pencil: it turns the dropdown into a text input for up
  to 8 printable ASCII characters; Enter applies it live, Escape cancels, and it stays unsaved until you save),
  to download the preset as a JSON file (the backup format of the CLI) and to upload such a file. An upload asks
  first, applies the file's content to the active preset live (whatever preset the file came from) and leaves it
  unsaved; the server's message is shown if the file is refused. Download needs the pedal, upload also needs a
  server that is not read-only.
- Top right: the Connection menu (its button is green, amber or red with the device state), a
  light/dark switch and the language. A "Read-only" badge appears when the server is read-only.
- A floating tuning-fork button turns the pedal's tuner on and off (CC 80). A floating gear button to its left
  opens the pedal's own settings in a panel at the bottom left: loopback, input gain (-20 to 20 dB), USB and
  Bluetooth volume, MIDI channel (Omni or 1 to 16) and the pedal's Bluetooth switch (turning it off drops the
  Bluetooth connection). They are read when the panel opens and written as you change them (sliders after
  150 ms without movement); the pedal keeps them itself, so there is no save button and they are not part of any
  preset. The controls are disabled when the server is read-only or the pedal is not connected.
- A third floating button (stacked layers, to the left of the gear) opens the models panel at the bottom left; only
  one of the settings and models panels is open at a time. Its two tabs, Amplifiers and Cabinets (IR), list the slots
  stored in the pedal (number, name, the active one marked; amplifiers 30 to 37 are drive models). Each row has a
  download button (saves the verified slot as a `.bin` file) and an upload button (an IR takes a `.wav` or `.bin`, an
  amplifier only a `.bin` that was read from a pedal; an `.ead` file is installed only through a decryptor you configure on the server, see `docs/ead-decryptor.md`). An upload asks
  first, then writes and verifies the slot; the server keeps the old content in a backup file on the computer that runs
  it. Writing a slot is not an edit of the preset: nothing becomes unsaved. The server's message is shown if a file is
  refused. Download needs the pedal, upload also needs a server that is not read-only. The panel is read from the
  pedal each time it opens (about one query per slot, so it takes a moment).

## The measured mapping

The pedal reports its state as a list of eight entries with normalized parameter values. How
those entries line up with the editor's blocks, types and controls was measured on a real
pedal (the developer's, firmware as of the capture date recorded in the file) and is stored in
`src/data/measuredMapping.json` (also `docs/calibration/mapping.json`). Do not edit it by
hand: `scripts/calibrate_mapping.py` runs the measurements against a pedal and
`scripts/build_mapping.py` generates the file. `src/data/deviceMapping.ts` only reads it.

What it encodes: the entry order (`fx1, fx2, amp, cab, del, mod, rev, eq`; del before mod), the
variant the pedal reports for each editor type (reverb variants are 0,1,2,3,5,6,7), and for each
snapshot parameter the controller that changes it. Values are linear in the controller, so
`cc = round(normalized * 127)` goes through the editor's own conversions. A snapshot parameter
without a controller is not shown. An editor control with no snapshot parameter (for example
the delay, cab and reverb levels) stays editable, writes go out as CC as before, but its value
cannot be read back; it keeps its local value and carries a "not readable from the pedal" hint.

"firmware differs from the measured one": if a block reports a different number of parameters
than the table says, or a variant the table does not know, that block's parameter controls are
hidden and it shows this badge, because the numbers could not be trusted. Other blocks are not
affected.

Still assumed and unverified: the `chain_order` values use the editor's own block numbering
(fx1 0, fx2 1, amp 2, cab 3, mod 4, del 5, rev 6, eq 7), and the amp and cab slot numbers equal
the editor's model indexes.
