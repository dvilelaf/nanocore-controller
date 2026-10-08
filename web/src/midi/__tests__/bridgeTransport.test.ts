import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BridgeTransport, UnsupportedSysExError, decodeEditorSysEx } from '../bridgeTransport';
import type { BridgeErrorInfo, ServerState } from '../bridgeTransport';
import { buildAmpModelSysEx, buildBlockTypeSysEx, buildCabModelSysEx, buildChainOrderSysEx } from '../sysex';
import { FakeWebSocket, makeFetch, makeState } from '../../test/bridgeFakes';
import type { Call } from '../../test/bridgeFakes';

const TOKEN = 'secret-token-123';

function setup(handler?: (call: Call) => { status?: number; body: unknown }, opts = {}) {
  const state = makeState();
  const f = makeFetch(
    handler ??
      ((call) => {
        if (call.path === '/api/state') return { body: state };
        if (call.path === '/api/edit') return { status: 202, body: { applied: 1, rev: state.rev + 1 } };
        if (call.path === '/api/preset') return { body: { ...state, rev: 5 } };
        return { body: {} };
      }),
  );
  const bridge = new BridgeTransport({
    token: () => TOKEN,
    fetch: f.fn,
    WebSocket: FakeWebSocket,
    wsUrl: 'ws://127.0.0.1:1234/ws',
    ...opts,
  });
  const errors: BridgeErrorInfo[] = [];
  bridge.on('error', (e) => errors.push(e));
  return { bridge, f, errors, state };
}

async function connected(...args: Parameters<typeof setup>) {
  const s = setup(...args);
  await s.bridge.init();
  const ws = FakeWebSocket.last();
  ws.open();
  ws.receive({ type: 'state', rev: s.state.rev, state: s.state });
  s.f.calls.length = 0;
  return { ...s, ws };
}

const tick = (ms = 40) => vi.advanceTimersByTimeAsync(ms);

beforeEach(() => {
  vi.useFakeTimers();
  FakeWebSocket.reset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('BridgeTransport connection', () => {
  it('reads the state first, then authenticates the socket with a first message', async () => {
    const s = setup();
    await s.bridge.init();
    expect(s.f.calls.map((c) => c.path)).toEqual(['/api/state']);
    const ws = FakeWebSocket.last();
    expect(ws.url).toBe('ws://127.0.0.1:1234/ws');
    expect(ws.sent).toHaveLength(0);
    ws.open();
    expect(JSON.parse(ws.sent[0])).toEqual({ type: 'auth', token: TOKEN });
    ws.receive({ type: 'state', rev: 1, state: s.state });
    expect(s.bridge.getLink()).toBe('connected');
  });

  it('sends the token header on every /api call', async () => {
    const s = setup();
    await s.bridge.init();
    FakeWebSocket.last().open();
    s.bridge.sendCC('bridge', 1, 20, 127);
    await tick();
    await s.bridge.fetchPresets();
    await s.bridge.fetchAssets();
    expect(s.f.calls.map((c) => c.path)).toEqual(['/api/state', '/api/edit', '/api/presets', '/api/assets']);
    for (const call of s.f.calls) expect(call.headers['X-Nanocore-Token']).toBe(TOKEN);
  });

  it('never stores or logs the token', async () => {
    const log = vi.spyOn(console, 'log');
    const warn = vi.spyOn(console, 'warn');
    const error = vi.spyOn(console, 'error');
    const { bridge } = await connected();
    bridge.sendCC('bridge', 1, 20, 127);
    await tick();
    const logged = JSON.stringify([...log.mock.calls, ...warn.mock.calls, ...error.mock.calls]);
    expect(logged).not.toContain(TOKEN);
    expect(JSON.stringify({ ...localStorage })).not.toContain(TOKEN);
  });

  it('starts with an empty token when the server needs none', async () => {
    const { bridge, f } = setup(undefined, { token: () => '' });
    await bridge.init();
    expect(f.calls[0].headers['X-Nanocore-Token']).toBe('');
  });

  it('refuses to start without a token', async () => {
    const bridge = new BridgeTransport({ token: () => null, fetch: makeFetch(() => ({ body: {} })).fn });
    await expect(bridge.init()).rejects.toMatchObject({ code: 'no_token' });
  });

  it('reconnects the socket with growing delays and re-authenticates', async () => {
    const { ws } = await connected();
    ws.drop(1006);
    expect(FakeWebSocket.instances).toHaveLength(1);
    await tick(499);
    expect(FakeWebSocket.instances).toHaveLength(1);
    await tick(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    FakeWebSocket.last().drop(1006);
    await tick(999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    await tick(1);
    expect(FakeWebSocket.instances).toHaveLength(3);
    FakeWebSocket.last().open();
    expect(JSON.parse(FakeWebSocket.last().sent[0]).type).toBe('auth');
  });

  it('reports link changes and does not reconnect after a 4401 close', async () => {
    const { bridge, ws, errors } = await connected();
    const links: string[] = [];
    bridge.on('link', (l) => links.push(l));
    ws.drop(4401);
    await tick(20000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(errors[0].code).toBe('unauthorized');
    expect(links).toEqual(['closed']);
  });

  it('re-reads the state when a patch skips a revision, and applies contiguous ones', async () => {
    const { bridge, f, ws } = await connected();
    const patches: number[] = [];
    bridge.on('patch', (rev) => patches.push(rev));
    ws.receive({ type: 'patch', rev: 2, ops: [{ op: 'volume', value: 50 }] });
    expect(patches).toEqual([2]);
    ws.receive({ type: 'patch', rev: 2, ops: [] }); // stale
    expect(patches).toEqual([2]);
    ws.receive({ type: 'patch', rev: 4, ops: [{ op: 'volume', value: 60 }] });
    await tick();
    expect(patches).toEqual([2]);
    expect(f.calls.filter((c) => c.path === '/api/state')).toHaveLength(1);
  });
});

describe('BridgeTransport outgoing mapping', () => {
  it('turns CC into cc operations and coalesces repeated CCs inside the window', async () => {
    const { bridge, f } = await connected();
    bridge.sendCC('bridge', 1, 60, 10);
    bridge.sendCC('bridge', 1, 61, 5);
    bridge.sendCC('bridge', 1, 60, 99);
    await tick(32);
    expect(f.calls).toHaveLength(0);
    await tick(2);
    expect(f.calls).toHaveLength(1);
    expect(f.calls[0].path).toBe('/api/edit');
    expect((f.calls[0].body as { ops: unknown[] }).ops).toEqual([
      { op: 'cc', cc: 61, value: 5 },
      { op: 'cc', cc: 60, value: 99 },
    ]);
    bridge.sendCC('bridge', 1, 60, 1);
    await tick();
    expect(f.calls).toHaveLength(2);
  });

  it('keeps order across kinds of operation in one batch', async () => {
    const { bridge, f } = await connected();
    bridge.sendCC('bridge', 1, 20, 127);
    bridge.sendSysEx('bridge', buildChainOrderSysEx([1, 0, 2, 3, 4, 5, 6, 7]));
    bridge.sendCC('bridge', 1, 21, 0);
    await tick();
    expect((f.calls[0].body as { ops: unknown[] }).ops).toEqual([
      { op: 'cc', cc: 20, value: 127 },
      { op: 'chain_order', order: [1, 0, 2, 3, 4, 5, 6, 7] },
      { op: 'cc', cc: 21, value: 0 },
    ]);
  });

  it('sends at most 64 operations per request', async () => {
    const { bridge, f } = await connected();
    for (let cc = 0; cc < 70; cc++) bridge.sendCC('bridge', 1, cc, 1);
    await tick();
    const sizes = f.calls.map((c) => (c.body as { ops: unknown[] }).ops.length);
    expect(sizes).toEqual([64, 6]);
  });

  it('rejects CC values that are not integers 0..127', async () => {
    const { bridge, f } = await connected();
    expect(() => bridge.sendCC('bridge', 1, 20, 1.5)).toThrow(RangeError);
    expect(() => bridge.sendCC('bridge', 1, 20, 128)).toThrow(RangeError);
    expect(() => bridge.sendCC('bridge', 1, 200, 1)).toThrow(RangeError);
    await tick();
    expect(f.calls).toHaveLength(0);
  });

  it('maps program change to a preset recall with display number program + 1', async () => {
    const { bridge, f } = await connected();
    const states: ServerState[] = [];
    bridge.on('state', (s) => states.push(s));
    bridge.sendProgramChange('bridge', 1, 8);
    await tick();
    expect(f.calls.at(-1)).toMatchObject({ method: 'POST', path: '/api/preset', body: { display_number: 9 } });
    expect(states).toHaveLength(1);
  });

  it('flushes pending edits before recalling a preset', async () => {
    const { bridge, f } = await connected();
    bridge.sendCC('bridge', 1, 20, 127);
    bridge.sendProgramChange('bridge', 1, 0);
    await tick();
    expect(f.calls.map((c) => c.path)).toEqual(['/api/edit', '/api/preset']);
  });

  it('decodes the three editor SysEx frames', async () => {
    const { bridge, f } = await connected();
    bridge.sendSysEx('bridge', buildAmpModelSysEx(12));
    bridge.sendSysEx('bridge', buildCabModelSysEx(2));
    bridge.sendSysEx('bridge', buildChainOrderSysEx([0, 1, 2, 3, 5, 4, 6, 7]));
    await tick();
    expect((f.calls[0].body as { ops: unknown[] }).ops).toEqual([
      { op: 'amp', slot: 12 },
      { op: 'ir', slot: 2 },
      { op: 'chain_order', order: [0, 1, 2, 3, 5, 4, 6, 7] },
    ]);
  });

  it('refuses every other SysEx message and sends nothing', async () => {
    const { bridge, f } = await connected();
    const bad: number[][] = [
      buildBlockTypeSysEx(0x09, 1),
      buildBlockTypeSysEx(0x06, 99),
      buildChainOrderSysEx([0, 0, 2, 3, 4, 5, 6, 7]),
      [0xf0, 0x7e, 0x7f, 0x06, 0x01, 0xf7],
      [],
      buildAmpModelSysEx(1).slice(0, -1),
      [...buildAmpModelSysEx(1).slice(0, -1), 0x05, 0xf7],
    ];
    for (const bytes of bad) expect(() => bridge.sendSysEx('bridge', bytes)).toThrow(UnsupportedSysExError);
    await tick(200);
    expect(f.calls).toHaveLength(0);
    expect(() => decodeEditorSysEx([0xf0, 0xf7])).toThrow();
  });

  it('logs sent messages for the activity log', async () => {
    const { bridge } = await connected();
    const seen: string[] = [];
    bridge.onMessageSent((m) => seen.push(m.kind));
    bridge.sendCC('bridge', 1, 20, 127);
    bridge.sendSysEx('bridge', buildAmpModelSysEx(1));
    await tick();
    expect(seen).toEqual(['cc', 'sysex']);
  });
});

describe('BridgeTransport error codes', () => {
  const cases: [number, string, boolean?][] = [
    [401, 'unauthorized'],
    [403, 'read_only'],
    [409, 'slot_changed'],
    [503, 'disconnected'],
    [504, 'device_timeout', true],
    [400, 'validation'],
    [502, 'device_status'],
  ];

  for (const [status, code, maybe] of cases) {
    it(`reports ${status} ${code} and does not lose the edit silently`, async () => {
      const state = makeState();
      const { bridge, f, errors } = await connected((call) => {
        if (call.path === '/api/edit') {
          return { status, body: { error: { code, message: `m-${code}`, ...(maybe ? { maybe_applied: true } : {}) } } };
        }
        return { body: state };
      });
      bridge.sendCC('bridge', 1, 20, 127);
      await tick();
      expect(errors).toHaveLength(1);
      expect(errors[0]).toMatchObject({ code, status, context: 'edit', lost: true, maybeApplied: maybe === true });
      const rereads = f.calls.filter((c) => c.path === '/api/state').length;
      expect(rereads).toBe(code === 'unauthorized' ? 0 : 1);
    });
  }

  it('retries a rate limited batch and succeeds, reporting the slowdown', async () => {
    let attempts = 0;
    const state = makeState();
    const { bridge, f, errors } = await connected((call) => {
      if (call.path === '/api/edit') {
        attempts++;
        return attempts === 1
          ? { status: 429, body: { error: { code: 'rate_limited', message: 'slow' } } }
          : { status: 202, body: { applied: 1, rev: 3 } };
      }
      return { body: state };
    });
    bridge.sendCC('bridge', 1, 20, 127);
    await tick();
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatchObject({ code: 'rate_limited', lost: false });
    await tick(600);
    expect(attempts).toBe(2);
    expect((f.calls[f.calls.length - 1].body as { ops: unknown[] }).ops).toEqual([{ op: 'cc', cc: 20, value: 127 }]);
  });

  it('gives up on a persistently rate limited batch and says it was lost', async () => {
    const state = makeState();
    const { bridge, errors } = await connected(
      (call) =>
        call.path === '/api/edit'
          ? { status: 429, body: { error: { code: 'rate_limited', message: 'slow' } } }
          : { body: state },
    );
    bridge.sendCC('bridge', 1, 20, 127);
    await tick(20000);
    expect(errors.at(-1)).toMatchObject({ code: 'rate_limited', lost: true });
  });

  it('reports a network failure', async () => {
    const { bridge, errors } = await connected((call) => {
      if (call.path === '/api/edit') throw new Error('boom');
      return { body: makeState() };
    });
    bridge.sendCC('bridge', 1, 20, 127);
    await tick();
    expect(errors[0]).toMatchObject({ code: 'network', lost: true });
  });

  it('reports an error message pushed over the socket and re-reads the state', async () => {
    const { f, ws, errors } = await connected();
    ws.receive({ type: 'error', error: { code: 'device_timeout', message: 'x', maybe_applied: true } });
    await tick();
    expect(errors[0]).toMatchObject({ code: 'device_timeout', maybeApplied: true });
    expect(f.calls.filter((c) => c.path === '/api/state')).toHaveLength(1);
  });

  it('forwards autosave and connection messages', async () => {
    const { bridge, ws } = await connected();
    const seen: string[] = [];
    bridge.on('autosave', (a) => seen.push(`a:${a.state}`));
    bridge.on('connection', (c) => seen.push(`c:${c.connected}`));
    ws.receive({ type: 'autosave', state: 'saving', detail: null });
    ws.receive({ type: 'connection', connected: false, reason: 'lost' });
    expect(seen).toEqual(['a:saving', 'c:false']);
  });
});
