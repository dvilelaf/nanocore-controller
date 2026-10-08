import { beforeEach, describe, expect, it } from 'vitest';
import { BridgeTransport } from '../bridgeTransport';
import { FakeWebSocket, makeFetch, makeState } from '../../test/bridgeFakes';

const TOKEN = 'secret-token-123';

async function connected() {
  const f = makeFetch((call) => (call.path === '/api/edit' ? { status: 202, body: { applied: 1, rev: 2 } } : { body: makeState() }));
  const bridge = new BridgeTransport({ token: () => TOKEN, fetch: f.fn, WebSocket: FakeWebSocket, wsUrl: 'ws://127.0.0.1:1/ws' });
  await bridge.init();
  f.calls.length = 0;
  return { bridge, f };
}

beforeEach(() => FakeWebSocket.reset());

describe('BridgeTransport preset name', () => {
  it('sends a rename as a name edit through the edit queue and resolves when the server answered', async () => {
    const { bridge, f } = await connected();
    await bridge.renamePreset('Rock');
    expect(f.calls).toHaveLength(1);
    expect(f.calls[0]).toMatchObject({ method: 'POST', path: '/api/edit' });
    expect((f.calls[0].body as { ops: unknown[] }).ops).toEqual([{ op: 'name', name: 'Rock' }]);
    expect(f.calls[0].headers['X-Nanocore-Token']).toBe(TOKEN);
  });

  it('keeps the order of the edits that were queued before it', async () => {
    const { bridge, f } = await connected();
    bridge.sendCC('bridge', 1, 20, 127);
    await bridge.renamePreset('Rock');
    const ops = f.calls.flatMap((c) => (c.body as { ops: { op: string }[] }).ops.map((o) => o.op));
    expect(ops).toEqual(['cc', 'name']);
  });
});
