import { describe, expect, it } from 'vitest';
import { chainOrderToBlockIds } from '../bridgeTransport';
import { blockIdsToChainOrder } from '../../data/deviceMapping';
import { CHAIN_ORDER_BLOCK_IDS } from '../sysex';

// Checked on a real pedal against the official app's chain screen (2026-10-08): the order [0..7] shows
// FX1 FX2 AMP CAB DEL MOD REV EQ, and [7,6,5,4,3,2,1,0] shows EQ REV MOD DEL CAB AMP FX2 FX1.
const PEDAL_ORDER = ['fx1', 'fx2', 'amp', 'cab', 'del', 'mod', 'rev', 'eq'];

describe('chain order numbering', () => {
  it('numbers the blocks like the pedal: the entry order of its state, with the delay before the modulation', () => {
    expect(PEDAL_ORDER.map((id) => CHAIN_ORDER_BLOCK_IDS[id])).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  });

  it('reads the pedal order [0..7] as FX1 FX2 AMP CAB DEL MOD REV EQ', () => {
    expect(chainOrderToBlockIds([0, 1, 2, 3, 4, 5, 6, 7])).toEqual(PEDAL_ORDER);
  });

  it('reads the reversed order as the official app shows it', () => {
    expect(chainOrderToBlockIds([7, 6, 5, 4, 3, 2, 1, 0])).toEqual([...PEDAL_ORDER].reverse());
  });

  it('sends the modulation before the delay as [.. 5, 4 ..], which the app showed as MOD DEL', () => {
    expect(blockIdsToChainOrder(['fx1', 'fx2', 'amp', 'cab', 'mod', 'del', 'rev', 'eq'])).toEqual([0, 1, 2, 3, 5, 4, 6, 7]);
  });
});
