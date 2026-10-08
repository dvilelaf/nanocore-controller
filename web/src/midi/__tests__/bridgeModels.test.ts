import { beforeEach, describe, expect, it } from 'vitest';
import { BridgeTransport } from '../bridgeTransport';
import type { ModelList } from '../bridgeTransport';
import { FakeWebSocket, makeFetch, makeState } from '../../test/bridgeFakes';
import type { Call } from '../../test/bridgeFakes';

const TOKEN = 'secret-token-123';
const list: ModelList = {
  amp: [{ slot: 12, name: 'MesR2', size: 12242, crc32: 1, active: true }],
  ir: [{ slot: 2, name: 'Eng412A', size: 4096, crc32: 2, active: false }],
};

async function connected(
  handler: (call: Call) => { status?: number; body: unknown; bytes?: Uint8Array; headers?: Record<string, string> },
) {
  const f = makeFetch((call) => (call.path.startsWith('/api/models') ? handler(call) : { body: makeState() }));
  const bridge = new BridgeTransport({ token: () => TOKEN, fetch: f.fn, WebSocket: FakeWebSocket, wsUrl: 'ws://127.0.0.1:1/ws' });
  await bridge.init();
  f.calls.length = 0;
  return { bridge, f };
}

beforeEach(() => FakeWebSocket.reset());

describe('BridgeTransport models', () => {
  it('lists the slots of both storages with the token', async () => {
    const { bridge, f } = await connected(() => ({ body: list }));
    expect(await bridge.fetchModels()).toEqual(list);
    expect(f.calls).toHaveLength(1);
    expect(f.calls[0]).toMatchObject({ method: 'GET', path: '/api/models' });
    expect(f.calls[0].headers['X-Nanocore-Token']).toBe(TOKEN);
  });

  it('throws the server error of a failed listing', async () => {
    const { bridge } = await connected(() => ({ status: 503, body: { error: { code: 'disconnected', message: 'no link to the pedal' } } }));
    await expect(bridge.fetchModels()).rejects.toMatchObject({ code: 'disconnected', status: 503 });
  });

  it('downloads a slot as a Blob with the name the server offers', async () => {
    const bytes = new Uint8Array([68, 68, 80, 66, 1, 2, 3]);
    const { bridge, f } = await connected(() => ({
      body: null,
      bytes,
      headers: { 'Content-Disposition': 'attachment; filename="amp-12-MesR2.bin"' },
    }));
    const result = await bridge.downloadModel('amp', 12);
    expect(result.filename).toBe('amp-12-MesR2.bin');
    expect(new Uint8Array(await result.blob.arrayBuffer())).toEqual(bytes);
    expect(f.calls[0]).toMatchObject({ method: 'GET', path: '/api/models/amp/12' });
    expect(f.calls[0].headers['X-Nanocore-Token']).toBe(TOKEN);
  });

  it('falls back to a plain file name when the header is missing or odd', async () => {
    for (const header of [undefined, 'attachment; filename="../../etc/passwd"', 'attachment; filename=".hidden"']) {
      const { bridge } = await connected(() => ({ body: null, bytes: new Uint8Array([1]), headers: header ? { 'Content-Disposition': header } : undefined }));
      const { filename } = await bridge.downloadModel('ir', 3);
      expect(filename).toBe('ir-3.bin');
    }
  });

  it('throws the server error of a failed download', async () => {
    const { bridge } = await connected(() => ({ status: 502, body: { error: { code: 'protocol', message: 'checksum mismatch' } } }));
    await expect(bridge.downloadModel('amp', 5)).rejects.toMatchObject({ code: 'protocol', status: 502, message: 'checksum mismatch' });
  });

  it('uploads the raw file as the body, not as JSON', async () => {
    const answer = { slot: 29, name: 'IR29', size: 8, crc32: 5, active: false };
    const { bridge, f } = await connected(() => ({ body: answer }));
    const file = new File([new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8])], 'mine.bin');
    expect(await bridge.uploadModel('ir', 29, file)).toEqual(answer);
    expect(f.calls).toHaveLength(1);
    expect(f.calls[0]).toMatchObject({ method: 'POST', path: '/api/models/ir/29' });
    expect(f.calls[0].body).toBe(file);
    expect(f.calls[0].headers['Content-Type']).toBe('application/octet-stream');
    expect(f.calls[0].headers['X-Nanocore-Token']).toBe(TOKEN);
  });

  it('throws the message of a refused upload', async () => {
    const { bridge } = await connected(() => ({
      status: 400,
      body: { error: { code: 'validation', message: 'IR data must be a whole number of float32 samples' } },
    }));
    await expect(bridge.uploadModel('ir', 1, new File([new Uint8Array([1])], 'x.bin'))).rejects.toMatchObject({
      code: 'validation',
      status: 400,
      message: 'IR data must be a whole number of float32 samples',
    });
  });

  it('does not send a file over 4 MiB', async () => {
    const { bridge, f } = await connected(() => ({ body: {} }));
    const big = new File([new Uint8Array(4 * 1024 * 1024 + 1)], 'big.bin');
    await expect(bridge.uploadModel('ir', 1, big)).rejects.toMatchObject({ code: 'file_too_large' });
    expect(f.calls).toHaveLength(0);
  });
});
