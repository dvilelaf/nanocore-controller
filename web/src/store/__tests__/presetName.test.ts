import { beforeEach, describe, expect, it } from 'vitest';
import { applyOpsToDoc, useDeviceStore } from '../deviceState';
import { makeState } from '../../test/bridgeFakes';

const presets = [
  { slot: 8, display_number: 9, name: 'FunkCln' },
  { slot: 9, display_number: 10, name: 'Tremolo' },
];

beforeEach(() => useDeviceStore.getState().reset());

describe('the name operation', () => {
  it('changes the name of the active preset in the document and nothing else', () => {
    const doc = makeState();
    const next = applyOpsToDoc(doc, [{ op: 'name', name: 'Rock' }])!;
    expect(next.preset).toEqual({ slot: 8, display_number: 9, name: 'Rock' });
    expect(next.live).toEqual(doc.live);
    expect(doc.preset!.name).toBe('FunkCln');
  });

  it('shows the new name in the preset and in the list of the dropdown', () => {
    const store = useDeviceStore.getState();
    store.applyState(makeState());
    store.setPresets(presets);
    const snapshot = useDeviceStore.getState().applyOps([{ op: 'name', name: 'Rock' }]);
    expect(snapshot).not.toBeNull(); // no re-read of the state is needed
    const after = useDeviceStore.getState();
    expect(after.preset?.name).toBe('Rock');
    expect(after.presets.map((p) => p.name)).toEqual(['Rock', 'Tremolo']);
  });

  it('shows the stored name again when a whole state arrives (a restore, a recall)', () => {
    const store = useDeviceStore.getState();
    store.applyState(makeState());
    store.setPresets(presets);
    useDeviceStore.getState().applyOps([{ op: 'name', name: 'Rock' }]);
    useDeviceStore.getState().applyState(makeState());
    const after = useDeviceStore.getState();
    expect(after.preset?.name).toBe('FunkCln');
    expect(after.presets.map((p) => p.name)).toEqual(['FunkCln', 'Tremolo']);
  });
});
