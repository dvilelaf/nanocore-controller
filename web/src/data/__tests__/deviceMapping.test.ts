import { describe, expect, it } from 'vitest';
import measured from '../measuredMapping.json';
import {
  RUNTIME_BLOCK_ORDER,
  measuredType,
  readableParamIds,
  translateEffect,
  translateEffects,
  typeIdForVariant,
} from '../deviceMapping';
import { nanocoreSpec } from '../nanocoreSpec';
import { activeParams, findBlock } from '../../store/patchDefaults';
import type { ServerEffect } from '../../midi/bridgeTransport';

const effect = (index: number, variant: number, params: number[], enabled = true): ServerEffect => ({
  index,
  effect_id: index,
  enabled,
  variant,
  params,
});

const specCc = (blockId: string, typeId: number, cc: number) =>
  activeParams(findBlock(blockId), typeId).find((s) => s.cc === cc);

describe('measured table', () => {
  it('lists the snapshot blocks with del before mod', () => {
    expect(RUNTIME_BLOCK_ORDER).toEqual(['fx1', 'fx2', 'amp', 'cab', 'del', 'mod', 'rev', 'eq']);
    expect(nanocoreSpec.blocks.map((b) => b.id)).toEqual(['fx1', 'fx2', 'amp', 'cab', 'mod', 'del', 'rev', 'eq']);
  });

  it('puts effects on the right blocks by snapshot index', () => {
    const effects = RUNTIME_BLOCK_ORDER.map((_, i) => effect(i, 0, new Array(measuredType(RUNTIME_BLOCK_ORDER[i], 0)!.param_count).fill(0), i === 4));
    const { blocks } = translateEffects(effects);
    expect(blocks.del.on).toBe(true);
    expect(blocks.mod.on).toBe(false);
  });

  it('measures every editor type', () => {
    for (const block of nanocoreSpec.blocks) {
      for (const t of block.types) expect(measuredType(block.id, t.id), `${block.id}/${t.id}`).not.toBeNull();
    }
  });

  it('maps reverb variants 0,1,2,3,5,6,7 to the editor type ids 0..6', () => {
    const variants = findBlock('rev').types.map((t) => measuredType('rev', t.id)!.variant);
    expect(variants).toEqual([0, 1, 2, 3, 5, 6, 7]);
    expect(typeIdForVariant('rev', 5)).toBe(4);
    expect(typeIdForVariant('rev', 4)).toBeNull();
  });

  it('knows the table came from the generated file', () => {
    expect(measured.format).toBe('nanocore-runtime-mapping');
  });
});

describe('controller to value translation', () => {
  it('amp controllers 60..64 are snapshot parameters 0..4, converted with the editor ranges', () => {
    const { blocks } = translateEffects([effect(2, 0, [1, 0, 0.5, 1, 0])]);
    const p = blocks.amp.params!;
    expect(p.gain).toBeCloseTo(1);
    expect(p.bass).toBeCloseTo(-10);
    expect(p.mid).toBeCloseTo(0, 0);
    expect(p.treble).toBeCloseTo(10);
    expect(p.level).toBeCloseTo(-10);
    expect(blocks.amp.typeId).toBeUndefined();
  });

  it('cab filters use their real ranges', () => {
    const { blocks } = translateEffects([effect(3, 0, [0, 1])]);
    expect(blocks.cab.params).toEqual({ low_cut: 20, high_cut: 20000 });
  });

  it('mod Tremolo controllers 68..72 are parameters 0..4', () => {
    const m = measuredType('mod', 3)!;
    expect(m.params.map((p) => p.cc)).toEqual([68, 69, 70, 71, 72]);
    const result = translateEffect('mod', effect(5, 3, [1, 1, 1, 1, 1]));
    expect(result.typeId).toBe(3);
    for (const [j, cc] of [68, 69, 70, 71, 72].entries()) {
      const spec = specCc('mod', 3, cc)!;
      const only = [0, 0, 0, 0, 0];
      only[j] = 1;
      const one = translateEffect('mod', effect(5, 3, only));
      expect(one.params![spec.id], `cc ${cc}`).toBe(spec.kind === 'range' ? spec.max : spec.options.length - 1);
    }
  });

  it('8-band EQ controller 33 is snapshot parameter 7', () => {
    const m = measuredType('eq', 2)!;
    expect(m.params[7].cc).toBe(33);
    expect(m.params[8].cc).toBeNull();
    const spec = specCc('eq', 2, 33);
    if (spec) {
      const params = new Array(m.param_count).fill(0);
      params[7] = 1;
      const out = translateEffect('eq', effect(7, 2, params));
      expect(out.params![spec.id]).toBeCloseTo(spec.kind === 'range' ? spec.max : 0);
    }
  });

  it('the delay level of BBD is not in the snapshot: not read, not overwritten', () => {
    expect(readableParamIds('del', 0).has('level')).toBe(false);
    expect(readableParamIds('del', 0).has('mix')).toBe(true);
    const out = translateEffect('del', effect(4, 0, [0.5, 0.5, 0.5, 0.5]));
    expect(out.aligned).toBe(true);
    expect(out.params).not.toHaveProperty('level');
    expect(Object.keys(out.params!)).toHaveLength(4);
  });

  it('the second Gate parameter has no controller and is not displayed', () => {
    const out = translateEffect('fx1', effect(0, 0, [1, 1]));
    expect(Object.keys(out.params!)).toHaveLength(1);
  });

  it('cab level does not exist in the snapshot', () => {
    expect(readableParamIds('cab', 0)).toEqual(new Set(['low_cut', 'high_cut']));
  });
});

describe('firmware guard', () => {
  it('hides only the block whose parameter count differs', () => {
    const { blocks, unaligned } = translateEffects([effect(2, 0, [0.5, 0.5, 0.5, 0.5, 0.5]), effect(3, 0, [0.5, 0.5, 0.5])]);
    expect(unaligned).toEqual(['cab']);
    expect(blocks.cab.params).toBeUndefined();
    expect(blocks.amp.params).toBeDefined();
  });

  it('treats an unknown variant as a different firmware', () => {
    const out = translateEffect('rev', effect(6, 4, [0, 0, 0, 0, 0]));
    expect(out.aligned).toBe(false);
    expect(out.typeId).toBeUndefined();
    expect(out.params).toBeUndefined();
  });

  it('clamps values outside 0..1', () => {
    const out = translateEffect('amp', effect(2, 0, [-1, 2, 0, 0, 0]));
    expect(out.params!.gain).toBe(0);
    expect(out.params!.bass).toBeCloseTo(10);
  });
});
