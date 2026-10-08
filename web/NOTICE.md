# Origin and changes

This directory started as a copy of the `app/` folder of the unofficial
**NanoCore Editor** by Luca Nenni, <https://github.com/lucanenni/livtra-nanocore-editor>,
commit `fe742e2` ("Correct MIDI_MAPPING_NOTES.md: FunkCln was the active preset
name, not an FX1 type"), released under the MIT license (see `LICENSE`).

The project is independent of Livtra and is not endorsed by it. "NanoCore" and
other product names belong to their owners.

## Changes made in this repository

- `src/midi/bridgeTransport.ts`, `bridgeToken.ts`: a new `bridge` transport that talks to the
  local `nanocore serve` server instead of MIDI. The access token is taken from the URL
  fragment and kept in memory. Edits are coalesced and batched; the editor's three SysEx
  frames (chain order, amp model, cab model) are turned into server operations and any other
  SysEx is refused.
- `src/store/deviceState.ts`, `src/data/deviceMapping.ts`: the server's state document and the
  translation of the pedal's runtime snapshot into the editor's patch model, driven by the
  measured table `measuredMapping.json` with a per-block firmware guard. `applyRemoteState` in the patch store
  shows pedal state without sending anything back.
- `src/components/ChainBoard.tsx`, `BlockCard.tsx`: replace the tabbed `ChainView` and
  `BlockEditor` with one card per block, all parameters visible, drag-and-drop plus keyboard
  reordering. `ParamControl` gained an `idPrefix` so ids stay unique.
- `src/components/ConnectionStatus.tsx`: header indicators for the connection and the autosave
  state, and a translated error banner. No save button.
- `src/components/BridgePresets.tsx`: preset list read from the pedal.
- `ConnectionPanel`, `App`: the `bridge` transport, automatic connection when a token is
  present, and the "send patch" and local-library flows hidden in that mode.
- `vite.config.ts`: `base: './'` so the bundle works when served from the server's root.
- Test changes: `BlockEditor.test.tsx` became `BlockCard.test.tsx` (same every-type render
  check on the new component); new tests for the bridge, the mapping, the board, the status
  indicators and hostile device names.
- Locale files: English and Italian strings for the new UI.

The full history is in the git log of this repository.
