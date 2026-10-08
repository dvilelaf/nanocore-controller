import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BridgeTransport } from '../bridgeTransport';
import type { BridgeErrorInfo, GlobalSettings } from '../bridgeTransport';
import { FakeWebSocket, makeFetch, makeState } from '../../test/bridgeFakes';
import type { Call } from '../../test/bridgeFakes';

const TOKEN = 'secret-token-123';

const settings: GlobalSettings = {
  version: 1,
  wireless_enabled: true,
  loopback_enabled: false,
  input_gain_db: 1,
  usb_volume: 100,
  bt_volume: 100,
  midi_channel: 0,
  volume_floor: null,
  volume_ceiling: null,
};

async function connected(settingsReply: (call: Call) => { status?: number; body: unknown }) {
  const f = makeFetch((call) => (call.path === '/api/settings' ? settingsReply(call) : { body: makeState() }));
  const bridge = new BridgeTransport({ token: () => TOKEN, fetch: f.fn, WebSocket: FakeWebSocket, wsUrl: 'ws://127.0.0.1:1/ws' });
  const errors: BridgeErrorInfo[] = [];
  bridge.on('error', (e) => errors.push(e));
  await bridge.init();
  f.calls.length = 0;
  return { bridge, f, errors };
}

beforeEach(() => {
  FakeWebSocket.reset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('BridgeTransport global settings', () => {
  it('reads the settings with a GET and the token', async () => {
    const { bridge, f } = await connected(() => ({ body: settings }));
    await expect(bridge.fetchSettings()).resolves.toEqual(settings);
    expect(f.calls).toHaveLength(1);
    expect(f.calls[0]).toMatchObject({ method: 'GET', path: '/api/settings' });
    expect(f.calls[0].headers['X-Nanocore-Token']).toBe(TOKEN);
  });

  it('writes only the fields it is given and returns the new settings', async () => {
    const { bridge, f } = await connected(() => ({ body: { ...settings, input_gain_db: -5 } }));
    const result = await bridge.writeSettings({ input_gain_db: -5 });
    expect(result.input_gain_db).toBe(-5);
    expect(f.calls).toHaveLength(1);
    expect(f.calls[0]).toMatchObject({ method: 'POST', path: '/api/settings', body: { input_gain_db: -5 } });
    expect(f.calls[0].headers['X-Nanocore-Token']).toBe(TOKEN);
  });

  it('throws the server error of a refused write and leaves the edit error flow alone', async () => {
    const { bridge, errors } = await connected(() => ({
      status: 400,
      body: { error: { code: 'validation', message: 'usb_volume must be an integer from 0 to 100' } },
    }));
    await expect(bridge.writeSettings({ usb_volume: 500 })).rejects.toMatchObject({ code: 'validation', status: 400 });
    expect(errors).toHaveLength(0);
  });

  it('throws a network error when the server does not answer', async () => {
    const { bridge } = await connected(() => {
      throw new Error('boom');
    });
    await expect(bridge.fetchSettings()).rejects.toMatchObject({ code: 'network' });
  });
});
