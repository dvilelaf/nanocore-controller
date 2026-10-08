import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setEditClock, usePatchStore } from '../patchStore';
import { applyOpsToDoc, useDeviceStore } from '../deviceState';
import { captureTokenFromUrl, resetBridgeTokenForTests } from '../../midi/bridgeToken';
import { FakeWebSocket, makeFetch, makeState } from '../../test/bridgeFakes';
import type { Call } from '../../test/bridgeFakes';

function stateWith(effects: Record<number, { variant: number; params: number[]; enabled?: boolean }>) {
  const doc = makeState();
  for (const [i, e] of Object.entries(effects)) {
    doc.live!.effects[Number(i)] = { index: Number(i), effect_id: Number(i), enabled: e.enabled ?? true, ...e };
  }
  return doc;
}

let calls: Call[];

async function connect(doc = makeState()) {
  const f = makeFetch((call) => {
    if (call.path === '/api/assets') return { body: { amp: { slot: 12, name: 'MesR2' }, ir: null } };
    return { body: doc };
  });
  calls = f.calls;
  vi.stubGlobal('fetch', f.fn);
  vi.stubGlobal('WebSocket', FakeWebSocket);
  captureTokenFromUrl({ hash: '#token=tok', pathname: '/', search: '' }, { replaceState: vi.fn(), state: null });
  await usePatchStore.getState().initTransport('bridge');
  await new Promise((r) => setTimeout(r, 0));
}

beforeEach(() => {
  FakeWebSocket.reset();
  usePatchStore.getState().resetPatch();
});

afterEach(async () => {
  await usePatchStore.getState().initTransport('simulator');
  resetBridgeTokenForTests();
  vi.unstubAllGlobals();
});

describe('hydrating from the server', () => {
  it('shows the pedal state without sending anything back', async () => {
    const doc = stateWith({
      2: { variant: 0, params: [0.5, 0.5, 0.5, 0.5, 0.5] },
      4: { variant: 2, params: [0.1, 0.2, 0.3] },
      6: { variant: 5, params: [0.1, 0.2, 0.3, 0.4, 0.5] },
    });
    doc.live!.chain_order = [1, 0, 2, 3, 4, 5, 6, 7];
    await connect(doc);

    const { patch, chainOrder, log } = usePatchStore.getState();
    expect(patch.amp.on).toBe(true);
    expect(patch.amp.params.bass).toBeCloseTo(0, 0);
    expect(patch.rev).toMatchObject({ on: true, typeId: 4 });
    expect(patch.del).toMatchObject({ on: true, typeId: 2 });
    expect(chainOrder.slice(0, 2)).toEqual(['fx2', 'fx1']);
    expect(log).toHaveLength(0);
    expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(0);
    expect(useDeviceStore.getState().unaligned).toContain('del');
  });

  it('applies pushed patches and still sends nothing', async () => {
    const doc = stateWith({ 2: { variant: 0, params: [0.5, 0.5, 0.5, 0.5, 0.5] } });
    await connect(doc);
    const ws = FakeWebSocket.last();
    ws.open();
    ws.receive({ type: 'patch', rev: 2, ops: [{ op: 'param', effect: 2, index: 0, value: 1 }] });
    expect(usePatchStore.getState().patch.amp.params.gain).toBe(1);
    ws.receive({ type: 'patch', rev: 3, ops: [{ op: 'enabled', effect: 2, enabled: false }] });
    expect(usePatchStore.getState().patch.amp.on).toBe(false);
    expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(0);
  });

  it('re-reads the state when a patch carries an operation it cannot apply', async () => {
    await connect();
    const before = calls.filter((c) => c.path === '/api/state').length;
    FakeWebSocket.last().receive({ type: 'patch', rev: 2, ops: [{ op: 'cc', cc: 60, value: 3 }] });
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.filter((c) => c.path === '/api/state').length).toBe(before + 1);
  });

  it('loads the amp and cab slot names', async () => {
    await connect();
    expect(useDeviceStore.getState().assets?.amp?.name).toBe('MesR2');
    expect(usePatchStore.getState().patch.amp.typeId).toBe(12);
  });

  it('applyRemoteState never reaches the transport', () => {
    usePatchStore.getState().applyRemoteState({
      blocks: { amp: { on: true, typeId: 3, params: { gain: 0.9 } } },
      unaligned: [],
      chainOrder: null,
    });
    expect(usePatchStore.getState().patch.amp.params.gain).toBe(0.9);
    expect(usePatchStore.getState().log).toHaveLength(0);
  });

  it('sends edits made in the editor through the bridge as documented operations', async () => {
    await connect();
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      usePatchStore.getState().setBlockOn('fx1', true);
      usePatchStore.getState().moveBlockTo('eq', 0);
      await vi.advanceTimersByTimeAsync(50);
      const edit = calls.find((c) => c.path === '/api/edit')!;
      expect((edit.body as { ops: unknown[] }).ops).toEqual([
        { op: 'cc', cc: 20, value: 127 },
        { op: 'chain_order', order: [7, 0, 1, 2, 3, 4, 5, 6] },
      ]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('echo suppression', () => {
  let now = 1000;
  beforeEach(() => {
    now = 1000;
    setEditClock(() => now);
  });
  afterEach(() => setEditClock(() => Date.now()));

  const patchParam = (rev: number, index: number, value: number) =>
    FakeWebSocket.last().receive({ type: 'patch', rev, ops: [{ op: 'param', effect: 2, index, value }] });

  it('ignores a stale value for the control being edited, only for that control, and never sends', async () => {
    await connect(stateWith({ 2: { variant: 0, params: [0.5, 0.5, 0.5, 0.5, 0.5] } }));
    FakeWebSocket.last().open();
    usePatchStore.getState().setParam('amp', 'gain', 0.9);
    const sent = usePatchStore.getState().log.length;
    now = 1200;
    patchParam(2, 0, 0.1);
    patchParam(3, 1, 1);
    const { params } = usePatchStore.getState().patch.amp;
    expect(params.gain).toBe(0.9);
    expect(params.bass).toBeCloseTo(10);
    expect(usePatchStore.getState().log.length).toBe(sent);
  });

  it('applies the server value again once 500 ms have passed', async () => {
    await connect(stateWith({ 2: { variant: 0, params: [0.5, 0.5, 0.5, 0.5, 0.5] } }));
    FakeWebSocket.last().open();
    usePatchStore.getState().setParam('amp', 'gain', 0.9);
    now = 1499;
    patchParam(2, 0, 0.2);
    expect(usePatchStore.getState().patch.amp.params.gain).toBe(0.9);
    now = 1500;
    patchParam(3, 0, 0.2);
    expect(usePatchStore.getState().patch.amp.params.gain).toBeCloseTo(0.2, 1);
  });

  it('does not suppress controls of other blocks or untouched controls', async () => {
    await connect(stateWith({ 2: { variant: 0, params: [0.5, 0.5, 0.5, 0.5, 0.5] } }));
    FakeWebSocket.last().open();
    usePatchStore.getState().setParam('cab', 'low_cut', 100);
    patchParam(2, 2, 1);
    expect(usePatchStore.getState().patch.amp.params.mid).toBeCloseTo(10);
  });
});

describe('unaligned flag after a local type change', () => {
  it('is recomputed from the last known parameter count', async () => {
    await connect(stateWith({ 4: { variant: 0, params: [0.1, 0.2, 0.3, 0.4] } }));
    expect(useDeviceStore.getState().unaligned).not.toContain('del');
    usePatchStore.getState().setBlockType('del', 1); // Digital has 3 parameters
    expect(useDeviceStore.getState().unaligned).toContain('del');
    usePatchStore.getState().setBlockType('del', 2); // Duck has 4
    expect(useDeviceStore.getState().unaligned).not.toContain('del');
  });

  it('is cleared when the count is unknown', async () => {
    await connect(stateWith({ 4: { variant: 0, params: [0.1, 0.2, 0.3, 0.4] } }));
    useDeviceStore.setState({ unaligned: ['del'], paramCounts: {} });
    usePatchStore.getState().setBlockType('del', 1);
    expect(useDeviceStore.getState().unaligned).not.toContain('del');
  });
});

describe('applyOpsToDoc', () => {
  it('refuses operations that do not fit the document', () => {
    const doc = makeState();
    expect(applyOpsToDoc(doc, [{ op: 'param', effect: 0, index: 3, value: 0.5 }])).toBeNull();
    expect(applyOpsToDoc({ ...doc, live: null }, [{ op: 'volume', value: 5 }])).toBeNull();
  });

  it('does not mutate the document it is given', () => {
    const doc = stateWith({ 2: { variant: 0, params: [0.5] } });
    const next = applyOpsToDoc(doc, [{ op: 'variant', effect: 2, variant: 3, params: [0.1] }])!;
    expect(next.live!.effects[2]).toMatchObject({ variant: 3, params: [0.1] });
    expect(doc.live!.effects[2]).toMatchObject({ variant: 0, params: [0.5] });
  });
});
