/**
 * NanoCore's SysEx protocol — reverse-engineered from real MIDI traffic captured between the
 * official Livtra "ToneCommand" app and a real NanoCore device (see docs/MIDI_MAPPING_NOTES.md
 * for the full write-up and the captured examples this is derived from). Not documented in the
 * manual or MIDI Control User Guide — the manual's own AMP/CAB type-select CCs (43/44) are
 * confirmed to have no effect on real hardware; SysEx is the only way to change these. AMP/CAB
 * model select and chain reorder are both confirmed working end-to-end against a real NanoCore.
 *
 * Frame: F0 7D 4E 43 <dir> <conn> 02 <seqLo> <seqHi> <opcode> 00 <len> 00 00 <fieldId>
 *        <value bytes...> F7
 *  - Manufacturer ID 0x7D ("non-commercial/educational", used by smaller companies without a
 *    registered MMA id) followed by an ASCII device tag "NC" (NanoCore).
 *  - <dir>: 0x70 host->device (all we send), 0x71 device->host ack (echoes <seqLo>/<seqHi>),
 *    0x72 unsolicited device->host push.
 *  - <conn>: observed as both 0x00 and 0x02 across different capture sessions with no apparent
 *    effect — likely a per-connection/session tag the device doesn't validate. We always send 0.
 *  - <seqLo>/<seqHi>: a per-message transaction id the device echoes back in its ack. Nothing
 *    suggests it's validated beyond that — a simple incrementing counter is enough.
 *  - opcode 0x6D = "set field": <fieldId> + <value bytes> follow a length prefix <len>.
 *    Confirmed field ids under this opcode:
 *      - 0x06 AMP model: 1 value byte, len=2 (1 for fieldId + 1 for the value — this is the
 *        general case, see buildBlockTypeSysEx). Confirmed with 4 isolated single-value captures
 *        across different models, cross-checked against `data/blocks/amp.ts`'s AMP model list —
 *        the value is the *same* 0-29 index already used there (and by the non-working typeCC).
 *      - 0x07 CAB model: same shape, confirmed with 3 isolated single-value captures against
 *        `data/blocks/cab.ts`'s CAB model list.
 *      - 0x05 effect chain order: value = [0x08, 0x00, <8 block-id bytes>] (10 bytes), with
 *        len=10 — i.e. NOT the "1 + value bytes" pattern the scalar fields above follow.
 *        Reproduced byte-for-byte from 4 captured examples (one isolated single-block drag, plus
 *        3 more from a longer session), and the block-id numbering (CHAIN_ORDER_BLOCK_IDS below)
 *        is confirmed correct on real hardware — moving a block in the editor moves the same
 *        block on the device's own Effect Chain screen. Still only understood empirically though:
 *        why the leading 0x08/0x00 pair and the different len convention exist isn't known, just
 *        that reproducing them verbatim works. See docs/MIDI_MAPPING_NOTES.md.
 */

const MANUFACTURER_ID = 0x7d;
const DEVICE_TAG = [0x4e, 0x43]; // "NC"
const DIR_HOST_TO_DEVICE = 0x70;
const CONNECTION_TAG = 0x00;
const SET_FIELD_OPCODE = 0x6d;

export const SYSEX_FIELD = {
  CHAIN_ORDER: 0x05,
  AMP_MODEL: 0x06,
  CAB_MODEL: 0x07,
} as const;

/** Block-id numbering for the chain-order array: the entry order of the pedal's own state
 * (`fx1, fx2, amp, cab, del, mod, rev, eq`), with the delay BEFORE the modulation. Checked on a real
 * pedal against the official app's chain screen: the order [0..7] shows FX1 FX2 AMP CAB DEL MOD REV EQ and
 * [5,4] at positions 4 and 5 puts the modulation before the delay. (The original editor numbered the
 * modulation 4 and the delay 5; that was wrong for this pedal.) */
export const CHAIN_ORDER_BLOCK_IDS: Record<string, number> = {
  fx1: 0,
  fx2: 1,
  amp: 2,
  cab: 3,
  del: 4,
  mod: 5,
  rev: 6,
  eq: 7,
};

let seq = 0;

/** Exposed for tests that need deterministic sequence numbers; not part of the public API. */
export function resetSysExSequence(): void {
  seq = 0;
}

function nextSeq(): [number, number] {
  seq = (seq + 1) & 0xff;
  return [seq, 0x00];
}

/** `len` defaults to the "scalar field" convention (1 for fieldId + the value bytes) — pass it
 * explicitly for fields that don't follow that convention (see buildChainOrderSysEx). */
function buildSetFieldSysEx(fieldId: number, valueBytes: number[], len = 1 + valueBytes.length): number[] {
  const [seqLo, seqHi] = nextSeq();
  return [
    0xf0,
    MANUFACTURER_ID,
    ...DEVICE_TAG,
    DIR_HOST_TO_DEVICE,
    CONNECTION_TAG,
    0x02,
    seqLo,
    seqHi,
    SET_FIELD_OPCODE,
    0x00,
    len,
    0x00,
    0x00,
    fieldId & 0x7f,
    ...valueBytes.map((b) => b & 0x7f),
    0xf7,
  ];
}

/** `modelIndex` is the same 0-29 index as `data/blocks/amp.ts`'s AMP model list. */
export function buildAmpModelSysEx(modelIndex: number): number[] {
  return buildSetFieldSysEx(SYSEX_FIELD.AMP_MODEL, [modelIndex]);
}

/** `modelIndex` is the same 0-29 index as `data/blocks/cab.ts`'s CAB model list. */
export function buildCabModelSysEx(modelIndex: number): number[] {
  return buildSetFieldSysEx(SYSEX_FIELD.CAB_MODEL, [modelIndex]);
}

/** Generic form of the two above, for any block with a `sysexTypeField` (see `data/types.ts`) —
 * `typeId` is sent verbatim as the single value byte, same index a working typeCC would use. */
export function buildBlockTypeSysEx(fieldId: number, typeId: number): number[] {
  return buildSetFieldSysEx(fieldId, [typeId]);
}

/** `order` = 8 block-ids (see CHAIN_ORDER_BLOCK_IDS) in the new chain position order. The 0x08/
 * 0x00 prefix and len=10 reproduce captured traffic byte-for-byte — see the file header for what
 * is and isn't actually confirmed about this one. */
export function buildChainOrderSysEx(order: number[]): number[] {
  const valueBytes = [0x08, 0x00, ...order];
  return buildSetFieldSysEx(SYSEX_FIELD.CHAIN_ORDER, valueBytes, valueBytes.length);
}
