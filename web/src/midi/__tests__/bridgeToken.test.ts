import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  captureTokenFromUrl,
  detectTokenlessServer,
  forgetTokenInMemoryForTests,
  getBridgeToken,
  hasBridgeToken,
  resetBridgeTokenForTests,
} from '../bridgeToken';

afterEach(() => resetBridgeTokenForTests());

describe('captureTokenFromUrl', () => {
  it('reads the token from the fragment, keeps it in memory and cleans the address bar', () => {
    const replaceState = vi.fn();
    const token = captureTokenFromUrl(
      { hash: '#token=abc123', pathname: '/', search: '?x=1' },
      { replaceState, state: { s: 1 } },
    );
    expect(token).toBe('abc123');
    expect(replaceState).toHaveBeenCalledWith({ s: 1 }, '', '/?x=1');
    expect(getBridgeToken()).toBe('abc123');
    expect(hasBridgeToken()).toBe(true);
  });

  it('keeps the token on a second call after the fragment is gone', () => {
    const hist = { replaceState: vi.fn(), state: null };
    captureTokenFromUrl({ hash: '#token=abc', pathname: '/', search: '' }, hist);
    expect(captureTokenFromUrl({ hash: '', pathname: '/', search: '' }, hist)).toBe('abc');
    expect(hist.replaceState).toHaveBeenCalledTimes(1);
  });

  it('keeps other fragment parameters', () => {
    const replaceState = vi.fn();
    captureTokenFromUrl({ hash: '#a=1&token=zzz', pathname: '/p', search: '' }, { replaceState, state: null });
    expect(replaceState).toHaveBeenCalledWith(null, '', '/p#a=1');
  });

  it('returns null and leaves the URL alone without a token', () => {
    const replaceState = vi.fn();
    expect(captureTokenFromUrl({ hash: '', pathname: '/', search: '' }, { replaceState, state: null })).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
    expect(hasBridgeToken()).toBe(false);
  });

  it('survives a page reload in the same tab through session storage', () => {
    captureTokenFromUrl({ hash: '#token=abc', pathname: '/', search: '' }, { replaceState: vi.fn(), state: null });
    forgetTokenInMemoryForTests(); // what a reload does: memory is gone, the tab's session storage is not
    expect(getBridgeToken()).toBeNull();
    const hist = { replaceState: vi.fn(), state: null };
    expect(captureTokenFromUrl({ hash: '', pathname: '/', search: '' }, hist)).toBe('abc');
    expect(hasBridgeToken()).toBe(true);
    expect(hist.replaceState).not.toHaveBeenCalled();
  });

  it('prefers the token in the address over an older stored one', () => {
    captureTokenFromUrl({ hash: '#token=old', pathname: '/', search: '' }, { replaceState: vi.fn(), state: null });
    forgetTokenInMemoryForTests();
    captureTokenFromUrl({ hash: '#token=new', pathname: '/', search: '' }, { replaceState: vi.fn(), state: null });
    forgetTokenInMemoryForTests();
    expect(captureTokenFromUrl({ hash: '', pathname: '/', search: '' })).toBe('new');
  });

  it('never writes the token to local storage', () => {
    captureTokenFromUrl({ hash: '#token=abc', pathname: '/', search: '' }, { replaceState: vi.fn(), state: null });
    expect(JSON.stringify({ ...localStorage })).not.toContain('abc');
  });

  it('works when session storage is blocked', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      expect(
        captureTokenFromUrl({ hash: '#token=abc', pathname: '/', search: '' }, { replaceState: vi.fn(), state: null }),
      ).toBe('abc');
    } finally {
      spy.mockRestore();
    }
  });
});

describe('detectTokenlessServer', () => {
  const health = (body: unknown, status = 200) =>
    vi.fn(async () => new Response(JSON.stringify(body), { status })) as unknown as typeof fetch;

  it('uses the server without a token when it says none is needed', async () => {
    const fetchFn = health({ ok: true, token_required: false });
    expect(await detectTokenlessServer(fetchFn)).toBe(true);
    expect(hasBridgeToken()).toBe(true);
    expect(getBridgeToken()).toBe('');
    expect(fetchFn).toHaveBeenCalledWith('/api/health', expect.anything());
    expect(JSON.stringify({ ...sessionStorage, ...localStorage })).not.toContain('nanocore-bridge-token');
  });

  it('does not connect when the server requires a token', async () => {
    expect(await detectTokenlessServer(health({ ok: true, token_required: true }))).toBe(false);
    expect(hasBridgeToken()).toBe(false);
  });

  it('does not connect to something that is not a NanoCore server', async () => {
    expect(await detectTokenlessServer(health({ ok: true }))).toBe(false);
    expect(await detectTokenlessServer(health({ nope: 1 }, 404))).toBe(false);
    const broken = vi.fn(async () => new Response('<html>', { status: 200 })) as unknown as typeof fetch;
    expect(await detectTokenlessServer(broken)).toBe(false);
    const down = vi.fn(async () => {
      throw new TypeError('network');
    }) as unknown as typeof fetch;
    expect(await detectTokenlessServer(down)).toBe(false);
    expect(hasBridgeToken()).toBe(false);
  });
});
