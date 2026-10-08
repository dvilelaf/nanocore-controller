import { beforeEach, describe, expect, it } from 'vitest';
import {
  buildAmpModelSysEx,
  buildBlockTypeSysEx,
  buildCabModelSysEx,
  buildChainOrderSysEx,
  resetSysExSequence,
  SYSEX_FIELD,
} from '../sysex';

/**
 * Byte-exact regression tests against real MIDI traffic captured (MIDI Monitor) between the
 * official ToneCommand app and a real NanoCore, isolating one action per capture — see
 * docs/MIDI_MAPPING_NOTES.md for the full write-up. The captured `<conn>` byte varied (0x00/
 * 0x02) across sessions with no apparent effect; we always send 0x00, so these examples reflect
 * that rather than the literal capture.
 */
describe('NanoCore SysEx builders (see docs/MIDI_MAPPING_NOTES.md)', () => {
  beforeEach(() => resetSysExSequence());

  it('builds an AMP model-select message matching a captured hardware example', () => {
    // Captured: 7d 4e 43 70 00 02 5b 00 6d 00 02 00 00 06 0f (AMP model 0x0f = "Pey51501")
    expect(buildAmpModelSysEx(0x0f)).toEqual([
      0xf0, 0x7d, 0x4e, 0x43, 0x70, 0x00, 0x02, 0x01, 0x00, 0x6d, 0x00, 0x02, 0x00, 0x00, 0x06, 0x0f, 0xf7,
    ]);
  });

  it('builds a CAB model-select message matching a captured hardware example', () => {
    // Captured: 7d 4e 43 70 02 02 0a 00 6d 00 02 00 00 07 13 (CAB model 0x13 = "Ran112B")
    expect(buildCabModelSysEx(0x13)).toEqual([
      0xf0, 0x7d, 0x4e, 0x43, 0x70, 0x00, 0x02, 0x01, 0x00, 0x6d, 0x00, 0x02, 0x00, 0x00, 0x07, 0x13, 0xf7,
    ]);
  });

  it('buildBlockTypeSysEx is the generic form of the AMP/CAB builders above', () => {
    resetSysExSequence();
    const generic = buildBlockTypeSysEx(SYSEX_FIELD.AMP_MODEL, 0x0f);
    resetSysExSequence();
    const specific = buildAmpModelSysEx(0x0f);
    expect(generic).toEqual(specific);
  });

  it('increments the sequence number on every call, wrapping at 256', () => {
    const first = buildAmpModelSysEx(0);
    const second = buildAmpModelSysEx(0);
    expect(first[7]).toBe(0x01);
    expect(second[7]).toBe(0x02);
  });

  it('builds a chain-order message matching a captured isolated single-block drag', () => {
    // Captured: 7d 4e 43 70 00 02 73 00 6d 00 0a 00 00 05 08 00 01 07 02 03 00 04 05 06
    expect(buildChainOrderSysEx([1, 7, 2, 3, 0, 4, 5, 6])).toEqual([
      0xf0, 0x7d, 0x4e, 0x43, 0x70, 0x00, 0x02, 0x01, 0x00, 0x6d, 0x00, 0x0a, 0x00, 0x00, 0x05, 0x08, 0x00, 0x01,
      0x07, 0x02, 0x03, 0x00, 0x04, 0x05, 0x06, 0xf7,
    ]);
  });

  it('masks out-of-range values to 7 bits rather than corrupting neighboring bytes', () => {
    const msg = buildAmpModelSysEx(255);
    expect(msg[14]).toBe(SYSEX_FIELD.AMP_MODEL); // fieldId untouched
    expect(msg[15]).toBe(255 & 0x7f); // value byte, masked
  });
});
