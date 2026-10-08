import { vi } from 'vitest';
import type { ServerState } from '../midi/bridgeTransport';

export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  sent: string[] = [];
  closed = false;

  readonly url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.closed = true;
  }

  open() {
    this.onopen?.({});
  }

  receive(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }

  drop(code = 1006) {
    this.onclose?.({ code });
  }

  static reset() {
    FakeWebSocket.instances = [];
  }

  static last(): FakeWebSocket {
    return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  }
}

export function makeState(over: Partial<ServerState> = {}): ServerState {
  return {
    rev: 1,
    connected: true,
    transport: 'bluetooth',
    read_only: false,
    preset: { slot: 8, display_number: 9, name: 'FunkCln' },
    live: {
      volume: 84,
      chain_order: [0, 1, 2, 3, 4, 5, 6, 7],
      effects: Array.from({ length: 8 }, (_, index) => ({
        index,
        effect_id: index,
        enabled: false,
        variant: 0,
        params: [],
      })),
    },
    autosave: { enabled: true, state: 'saved', last_saved_at: '2026-10-07T20:00:00+00:00', error: null },
    ...over,
  };
}

export interface Call {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

/** `bytes` answers with binary data instead of the JSON `body`. A raw (non-string) request body reaches the handler as it was sent. */
type Reply = { status?: number; body: unknown; bytes?: Uint8Array; headers?: Record<string, string> };

/** A stubbed fetch that records calls and answers from `handler`. */
export function makeFetch(handler: (call: Call) => Reply | Promise<Reply>) {
  const calls: Call[] = [];
  const fn = vi.fn(async (path: string, init?: RequestInit) => {
    const call: Call = {
      method: init?.method ?? 'GET',
      path,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? (typeof init.body === 'string' ? JSON.parse(init.body) : init.body) : undefined,
    };
    calls.push(call);
    const reply = await handler(call);
    return new Response(reply.bytes ? (reply.bytes as BodyInit) : JSON.stringify(reply.body), { status: reply.status ?? 200, headers: reply.headers });
  });
  return { fn: fn as unknown as typeof fetch, calls };
}
