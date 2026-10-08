import { beforeEach, describe, expect, it } from 'vitest';
import { BridgeTransport } from '../bridgeTransport';
import type { BridgeErrorInfo, ServerState } from '../bridgeTransport';
import { FakeWebSocket, makeFetch, makeState } from '../../test/bridgeFakes';
import type { Call } from '../../test/bridgeFakes';

const TOKEN = 'secret-token-123';
const file = { format: 'nanocore-controller-backup', version: 1, preset: { slot: 8, name: 'Fixture' } };

async function connected(handler: (call: Call) => { status?: number; body: unknown; headers?: Record<string, string> }) {
  const f = makeFetch((call) => (call.path === '/api/preset-file' ? handler(call) : { body: makeState() }));
  const bridge = new BridgeTransport({ token: () => TOKEN, fetch: f.fn, WebSocket: FakeWebSocket, wsUrl: 'ws://127.0.0.1:1/ws' });
  const errors: BridgeErrorInfo[] = [];
  const states: ServerState[] = [];
  bridge.on('error', (e) => errors.push(e));
  await bridge.init();
  bridge.on('state', (s) => states.push(s));
  f.calls.length = 0;
  return { bridge, f, errors, states };
}

beforeEach(() => FakeWebSocket.reset());

describe('BridgeTransport preset files', () => {
  it('downloads the preset file with the name the server offers', async () => {
    const { bridge, f } = await connected(() => ({
      body: file,
      headers: { 'Content-Disposition': 'attachment; filename="preset-09-FunkCln.json"' },
    }));
    const result = await bridge.downloadPresetFile();
    expect(result.filename).toBe('preset-09-FunkCln.json');
    expect(JSON.parse(result.text)).toEqual(file);
    expect(f.calls).toHaveLength(1);
    expect(f.calls[0]).toMatchObject({ method: 'GET', path: '/api/preset-file' });
    expect(f.calls[0].headers['X-Nanocore-Token']).toBe(TOKEN);
  });

  it('falls back to a plain name when the header is missing or odd', async () => {
    for (const header of [undefined, 'attachment; filename="../../etc/passwd"', 'attachment']) {
      const { bridge } = await connected(() => ({ body: file, headers: header ? { 'Content-Disposition': header } : undefined }));
      const { filename } = await bridge.downloadPresetFile();
      expect(filename).toMatch(/^[A-Za-z0-9_.-]+\.json$/);
      expect(filename).not.toContain('/');
    }
  });

  it('throws the server error of a failed download', async () => {
    const { bridge } = await connected(() => ({ status: 503, body: { error: { code: 'disconnected', message: 'no link to the pedal' } } }));
    await expect(bridge.downloadPresetFile()).rejects.toMatchObject({ code: 'disconnected', status: 503 });
  });

  it('uploads the document as the JSON body and takes the new state the server answers', async () => {
    const reply = makeState({ rev: 9, preset: { slot: 8, display_number: 9, name: 'Fixture' } });
    const { bridge, f, states } = await connected(() => ({ body: reply }));
    await bridge.uploadPresetFile(JSON.stringify(file));
    expect(f.calls).toHaveLength(1);
    expect(f.calls[0]).toMatchObject({ method: 'POST', path: '/api/preset-file', body: file });
    expect(f.calls[0].headers['Content-Type']).toBe('application/json');
    expect(states).toEqual([reply]);
  });

  it('waits for the edits queued before it', async () => {
    const { bridge, f } = await connected(() => ({ body: makeState() }));
    bridge.sendCC('bridge', 1, 20, 127);
    await bridge.uploadPresetFile(JSON.stringify(file));
    expect(f.calls.map((c) => c.path)).toEqual(['/api/edit', '/api/preset-file']);
  });

  it('rejects a file that is not JSON without calling the server', async () => {
    const { bridge, f } = await connected(() => ({ body: makeState() }));
    await expect(bridge.uploadPresetFile('not json {')).rejects.toMatchObject({ code: 'invalid_file' });
    expect(f.calls).toHaveLength(0);
  });

  it('throws the server message of a refused file (400) and leaves the state alone', async () => {
    const { bridge, f, states } = await connected(() => ({
      status: 400,
      body: { error: { code: 'validation', message: 'backup must contain exactly eight effects' } },
    }));
    await expect(bridge.uploadPresetFile(JSON.stringify(file))).rejects.toMatchObject({
      code: 'validation',
      status: 400,
      message: 'backup must contain exactly eight effects',
    });
    expect(f.calls.map((c) => c.path)).toEqual(['/api/preset-file']); // no re-read after a plain refusal
    expect(states).toHaveLength(0);
  });
});
