import { nanocoreSpec } from '../data/nanocoreSpec';
import { getBridgeToken } from './bridgeToken';
import { CHAIN_ORDER_BLOCK_IDS, SYSEX_FIELD } from './sysex';
import type { MessageListener, MidiPortInfo, MidiTransport, OutgoingMessage } from './types';

/** Shapes of docs/api.md that the editor consumes. */
export interface ServerEffect {
  index: number;
  effect_id: number;
  enabled: boolean;
  variant: number;
  params: number[];
}

export interface AutosaveInfo {
  enabled: boolean;
  state: 'saved' | 'dirty' | 'saving' | 'error';
  last_saved_at: string | null;
  error: string | null;
}

/** The speakers switch: `available` only when the server was started by the launcher that routes the audio. */
export interface AudioInfo {
  available: boolean;
  on: boolean;
}

export interface ServerState {
  rev: number;
  connected: boolean;
  /** The owner asked the server to let go of the pedal's USB port. Older servers do not send it. */
  released?: boolean;
  audio?: AudioInfo;
  transport?: string;
  read_only: boolean;
  preset: { slot: number; display_number: number; name: string } | null;
  live: { volume: number; chain_order: number[]; effects: ServerEffect[] } | null;
  autosave: AutosaveInfo;
}

export type EditOp =
  | { op: 'cc'; cc: number; value: number }
  | { op: 'param'; effect: number; index: number; value: number }
  | { op: 'enabled'; effect: number; enabled: boolean }
  | { op: 'variant'; effect: number; variant: number; params: number[] }
  | { op: 'volume'; value: number }
  | { op: 'name'; name: string }
  | { op: 'chain_order'; order: number[] }
  | { op: 'amp'; slot: number }
  | { op: 'ir'; slot: number };

export interface PresetListEntry {
  slot: number;
  display_number: number;
  name: string;
}

export interface AssetsInfo {
  amp: { slot: number; name: string } | null;
  ir: { slot: number; name: string } | null;
}

export type ModelKind = 'amp' | 'ir';

/** One occupied slot of the pedal's amplifier or IR storage (docs/api.md `GET /api/models`). */
export interface ModelEntry {
  slot: number;
  name: string;
  size: number;
  crc32: number;
  active: boolean;
}

export interface ModelList {
  amp: ModelEntry[];
  ir: ModelEntry[];
  /** True when the server has an .ead decryptor configured by the user. */
  ead?: boolean;
}

/** The server accepts a model file of at most 4 MiB. */
export const MAX_MODEL_BYTES = 4 * 1024 * 1024;

/** The pedal's own settings (docs/api.md `GET /api/settings`). They belong to no preset and need no save step. */
export interface GlobalSettings {
  version: number;
  wireless_enabled: boolean;
  loopback_enabled: boolean;
  /** -20..20 */
  input_gain_db: number;
  /** 0..100 */
  usb_volume: number;
  /** 0..100 */
  bt_volume: number;
  /** 0 = omni, 1..16 */
  midi_channel: number;
  volume_floor: number | null;
  volume_ceiling: number | null;
}

/** The fields `POST /api/settings` accepts; send only the ones that changed. */
export type SettingsChange = Partial<
  Pick<GlobalSettings, 'wireless_enabled' | 'loopback_enabled' | 'input_gain_db' | 'usb_volume' | 'bt_volume' | 'midi_channel'>
>;

export type BridgeLink = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed';

export type BridgeErrorContext = 'edit' | 'preset' | 'state' | 'socket' | 'save' | 'revert' | 'file';

export interface BridgeErrorInfo {
  /** Server code from docs/api.md, or `network` / `no_token` / `protocol` for client-side failures. */
  code: string;
  status: number;
  /** Server text. Shown only as a fallback: the UI translates by `code`. */
  message: string;
  maybeApplied: boolean;
  /** True when the edit that triggered the error was given up on instead of being retried. */
  lost: boolean;
  context: BridgeErrorContext;
}

export class BridgeApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly maybeApplied: boolean;

  constructor(code: string, status: number, message: string, maybeApplied = false) {
    super(message);
    this.name = 'BridgeApiError';
    this.code = code;
    this.status = status;
    this.maybeApplied = maybeApplied;
  }
}

export class UnsupportedSysExError extends Error {
  constructor(reason: string) {
    super(`Cannot send this SysEx message to the server: ${reason}`);
    this.name = 'UnsupportedSysExError';
  }
}

export interface BridgeEvents {
  state: (doc: ServerState) => void;
  patch: (rev: number, ops: EditOp[]) => void;
  autosave: (autosave: Partial<AutosaveInfo> & { state: AutosaveInfo['state'] }) => void;
  connection: (info: { connected: boolean; reason: string | null; released?: boolean }) => void;
  audio: (audio: AudioInfo) => void;
  link: (link: BridgeLink) => void;
  error: (error: BridgeErrorInfo) => void;
}

interface SocketLike {
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  send(data: string): void;
  close(): void;
}
type SocketCtor = new (url: string) => SocketLike;

export interface BridgeOptions {
  token?: () => string | null;
  fetch?: typeof fetch;
  WebSocket?: SocketCtor;
  wsUrl?: string;
  coalesceMs?: number;
  backoffMs?: number[];
  maxRateLimitRetries?: number;
}

const BRIDGE_OUTPUT: MidiPortInfo = { id: 'bridge', name: 'NanoCore server' };
const MAX_OPS_PER_REQUEST = 64;
const AMP_MODEL_COUNT = nanocoreSpec.blocks.find((b) => b.id === 'amp')!.types.length;
const CAB_MODEL_COUNT = nanocoreSpec.blocks.find((b) => b.id === 'cab')!.types.length;
const FRAME_HEADER = [0xf0, 0x7d, 0x4e, 0x43, 0x70, 0x00, 0x02];
const SET_FIELD_OPCODE = 0x6d;
const FIELD_INDEX = 14;

function isPermutation(values: number[], n: number): boolean {
  return values.length === n && new Set(values).size === n && values.every((v) => Number.isInteger(v) && v >= 0 && v < n);
}

/**
 * Turns the only three SysEx frames the editor builds (midi/sysex.ts) back into server
 * operations. Anything else is refused: raw SysEx never goes to the server.
 */
export function decodeEditorSysEx(bytes: number[]): EditOp {
  if (bytes.length < FIELD_INDEX + 2) throw new UnsupportedSysExError('frame too short');
  if (bytes[bytes.length - 1] !== 0xf7) throw new UnsupportedSysExError('missing end byte');
  if (!FRAME_HEADER.every((b, i) => bytes[i] === b)) throw new UnsupportedSysExError('not an editor frame');
  if (bytes[8] !== 0 || bytes[9] !== SET_FIELD_OPCODE || bytes[10] !== 0 || bytes[12] !== 0 || bytes[13] !== 0) {
    throw new UnsupportedSysExError('unknown opcode');
  }
  const len = bytes[11];
  const field = bytes[FIELD_INDEX];
  const value = bytes.slice(FIELD_INDEX + 1, -1);
  if (value.some((b) => b > 0x7f)) throw new UnsupportedSysExError('invalid data byte');

  if (field === SYSEX_FIELD.CHAIN_ORDER) {
    if (len !== 10 || value.length !== 10 || value[0] !== 8 || value[1] !== 0) {
      throw new UnsupportedSysExError('malformed chain order frame');
    }
    const order = value.slice(2);
    if (!isPermutation(order, 8)) throw new UnsupportedSysExError('chain order is not a permutation');
    return { op: 'chain_order', order };
  }
  if (field === SYSEX_FIELD.AMP_MODEL || field === SYSEX_FIELD.CAB_MODEL) {
    if (len !== 2 || value.length !== 1) throw new UnsupportedSysExError('malformed model frame');
    const limit = field === SYSEX_FIELD.AMP_MODEL ? AMP_MODEL_COUNT : CAB_MODEL_COUNT;
    if (value[0] >= limit) throw new UnsupportedSysExError('model index out of range');
    return { op: field === SYSEX_FIELD.AMP_MODEL ? 'amp' : 'ir', slot: value[0] };
  }
  throw new UnsupportedSysExError(`field 0x${field.toString(16)} is not supported`);
}

export function chainOrderToBlockIds(order: number[]): string[] | null {
  if (!isPermutation(order, 8)) return null;
  const byNumber = new Map(Object.entries(CHAIN_ORDER_BLOCK_IDS).map(([id, n]) => [n, id]));
  return order.map((n) => byNumber.get(n)!);
}

export class BridgeTransport implements MidiTransport {
  readonly kind = 'bridge' as const;
  readonly label = 'NanoCore server';

  private readonly opts: BridgeOptions;
  private readonly messageListeners = new Set<MessageListener>();
  private readonly handlers: { [K in keyof BridgeEvents]: Set<BridgeEvents[K]> } = {
    state: new Set(),
    patch: new Set(),
    autosave: new Set(),
    connection: new Set(),
    audio: new Set(),
    link: new Set(),
    error: new Set(),
  };

  private link: BridgeLink = 'idle';
  private token: string | null = null;
  private rev = 0;
  private clientSeq = 0;
  private queue: EditOp[] = [];
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private sending: Promise<void> = Promise.resolve();
  private rateLimitRetries = 0;
  private resyncing = false;
  private ws: SocketLike | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempt = 0;
  private generation = 0;

  constructor(opts: BridgeOptions = {}) {
    this.opts = opts;
  }

  listOutputs(): MidiPortInfo[] {
    return [BRIDGE_OUTPUT];
  }

  onPortsChanged(): () => void {
    return () => {};
  }

  onMessageSent(cb: MessageListener): () => void {
    this.messageListeners.add(cb);
    return () => this.messageListeners.delete(cb);
  }

  on<K extends keyof BridgeEvents>(event: K, cb: BridgeEvents[K]): () => void {
    const set = this.handlers[event] as Set<BridgeEvents[K]>;
    set.add(cb);
    return () => set.delete(cb);
  }

  getLink(): BridgeLink {
    return this.link;
  }

  async init(): Promise<void> {
    this.close();
    const generation = ++this.generation;
    this.token = (this.opts.token ?? getBridgeToken)();
    if (this.token === null) {
      throw new BridgeApiError('no_token', 401, 'No access token. Open the address printed by `nanocore serve`.');
    }
    this.setLink('connecting');
    try {
      await this.fetchState();
    } catch (err) {
      if (generation === this.generation) this.setLink('closed');
      throw err;
    }
    if (generation !== this.generation) return;
    this.openSocket();
  }

  close(): void {
    this.generation++;
    if (this.flushTimer) clearTimeout(this.flushTimer);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.flushTimer = null;
    this.reconnectTimer = null;
    this.queue = [];
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      ws.onmessage = null;
      ws.close();
    }
    if (this.link !== 'idle') this.setLink('idle');
  }

  sendCC(_outputId: string, channel: number, cc: number, value: number, description?: string): void {
    if (!Number.isInteger(cc) || cc < 0 || cc > 127) throw new RangeError(`CC number ${cc} is out of range`);
    if (!Number.isInteger(value) || value < 0 || value > 127) throw new RangeError(`CC value ${value} is out of range`);
    this.emitSent({ kind: 'cc', channel, cc, value, timestamp: performance.now(), description });
    this.enqueue({ op: 'cc', cc, value });
  }

  sendProgramChange(_outputId: string, channel: number, program: number, description?: string): void {
    if (!Number.isInteger(program) || program < 0 || program > 127) {
      throw new RangeError(`Program ${program} is out of range`);
    }
    this.emitSent({ kind: 'pc', channel, program, timestamp: performance.now(), description });
    void this.recallPreset(program + 1);
  }

  sendSysEx(_outputId: string, bytes: number[], description?: string): void {
    const op = decodeEditorSysEx(bytes);
    this.emitSent({ kind: 'sysex', bytes, timestamp: performance.now(), description });
    this.enqueue(op);
  }

  /** Sends everything queued right now. Resolves when the server has answered. */
  flush(): Promise<void> {
    if (this.flushTimer) clearTimeout(this.flushTimer);
    this.flushTimer = null;
    const ops = this.queue;
    this.queue = [];
    for (let i = 0; i < ops.length; i += MAX_OPS_PER_REQUEST) {
      const chunk = ops.slice(i, i + MAX_OPS_PER_REQUEST);
      this.sending = this.sending.then(() => this.postEdit(chunk));
    }
    return this.sending;
  }

  /** Loads a preset on the pedal. `discard` accepts losing edits that were not saved. */
  async recallPreset(displayNumber: number, discard = false): Promise<void> {
    await this.flush();
    try {
      const doc = await this.request<ServerState>('POST', '/api/preset', {
        display_number: displayNumber,
        discard,
      });
      this.acceptState(doc);
    } catch (err) {
      this.reportError(err, 'preset', false);
      await this.resync();
    }
  }

  /**
   * Renames the active preset in the pedal's working memory (1 to 8 printable ASCII characters; the caller
   * checks). Like any edit it leaves the preset unsaved until `saveNow`.
   */
  renamePreset(name: string): Promise<void> {
    this.enqueue({ op: 'name', name });
    return this.flush();
  }

  /** Sets the volume of the active preset (0 to 100) in the pedal's working memory; quick changes are merged. */
  setPresetVolume(value: number): void {
    this.enqueue({ op: 'volume', value });
  }

  /** Stores the edits of the active preset in the pedal's memory. */
  async saveNow(): Promise<void> {
    await this.flush();
    try {
      const res = await this.request<{ saved: boolean; autosave?: AutosaveInfo }>('POST', '/api/save-now');
      if (res.autosave) this.emit('autosave', res.autosave);
    } catch (err) {
      this.reportError(err, 'save', false);
      await this.resync();
    }
  }

  /** Reloads the stored version of the active preset, dropping the edits made since. */
  async revert(): Promise<void> {
    this.queue = [];
    if (this.flushTimer) clearTimeout(this.flushTimer);
    this.flushTimer = null;
    await this.sending;
    try {
      const doc = await this.request<ServerState>('POST', '/api/revert');
      this.acceptState(doc);
    } catch (err) {
      this.reportError(err, 'revert', false);
      await this.resync();
    }
  }

  /** Lets the server go of the pedal's USB port without stopping it: other programs can use the pedal. */
  async release(): Promise<void> {
    try {
      await this.request<{ released: boolean }>('POST', '/api/release');
      this.emit('connection', { connected: false, reason: null, released: true });
    } catch (err) {
      this.reportError(err, 'edit', false);
    }
  }

  /** Takes the pedal back; the server reconnects and sends the state. */
  async resume(): Promise<void> {
    try {
      await this.request<{ released: boolean }>('POST', '/api/resume');
    } catch (err) {
      this.reportError(err, 'edit', false);
    }
  }

  /** Turns the guitar through the PC speakers on or off (the launcher routes the audio). */
  async setAudio(on: boolean): Promise<void> {
    try {
      this.emit('audio', await this.request<AudioInfo>('POST', '/api/audio', { on }));
    } catch (err) {
      this.reportError(err, 'edit', false);
    }
  }

  async fetchPresets(): Promise<PresetListEntry[]> {
    const body = await this.request<{ presets: PresetListEntry[] }>('GET', '/api/presets');
    return body.presets;
  }

  async fetchAssets(): Promise<AssetsInfo> {
    return this.request<AssetsInfo>('GET', '/api/assets');
  }

  /** The active preset as the text of a JSON file in the backup format, with the file name the server proposes. */
  async downloadPresetFile(): Promise<{ filename: string; text: string }> {
    const res = await this.fetchOk('GET', '/api/preset-file');
    const named = /filename="([A-Za-z0-9_.-]+)"/.exec(res.headers.get('Content-Disposition') ?? '');
    const filename = named && !named[1].startsWith('.') ? named[1] : 'preset.json';
    return { filename, text: await res.text() };
  }

  /**
   * Applies the content of a preset file to the active preset, live (nothing is stored until it is saved).
   * Throws the server's refusal (400 with its message) or a network error; the page's state is then left alone.
   */
  async uploadPresetFile(text: string): Promise<void> {
    let document: unknown;
    try {
      document = JSON.parse(text);
    } catch {
      throw new BridgeApiError('invalid_file', 400, 'The file is not valid JSON.');
    }
    await this.flush();
    try {
      this.acceptState(await this.request<ServerState>('POST', '/api/preset-file', document));
    } catch (err) {
      // A refusal changed nothing. Anything else may have, so look at the pedal again.
      if (!(err instanceof BridgeApiError && err.status === 400)) await this.resync();
      throw err;
    }
  }

  /** The occupied slots of both model storages, read from the pedal (slow: one query per slot). */
  async fetchModels(): Promise<ModelList> {
    return this.request<ModelList>('GET', '/api/models');
  }

  /** The verified data of one slot, with the file name the server proposes. */
  async downloadModel(kind: ModelKind, slot: number): Promise<{ filename: string; blob: Blob }> {
    const res = await this.fetchOk('GET', `/api/models/${kind}/${slot}`);
    const named = /filename="([A-Za-z0-9_.-]+)"/.exec(res.headers.get('Content-Disposition') ?? '');
    const filename = named && !named[1].startsWith('.') ? named[1] : `${kind}-${slot}.bin`;
    return { filename, blob: await res.blob() };
  }

  /**
   * Writes a file into a slot (the server keeps the old content in a backup). The raw file is the request body; a
   * refusal is thrown with the server's message. Nothing here touches the preset or its saving state.
   */
  async uploadModel(kind: ModelKind, slot: number, file: Blob): Promise<ModelEntry> {
    if (file.size > MAX_MODEL_BYTES) throw new BridgeApiError('file_too_large', 413, 'The file is larger than 4 MiB.');
    const res = await this.fetchOk('POST', `/api/models/${kind}/${slot}`, undefined, file);
    return (await res.json()) as ModelEntry;
  }

  /** The pedal's global settings. Errors are thrown for the caller to show; they are not edit errors. */
  async fetchSettings(): Promise<GlobalSettings> {
    return this.request<GlobalSettings>('GET', '/api/settings');
  }

  /** Changes some global settings (only the given fields) and returns all of them as the pedal reports them. */
  async writeSettings(change: SettingsChange): Promise<GlobalSettings> {
    return this.request<GlobalSettings>('POST', '/api/settings', change);
  }

  async fetchState(): Promise<ServerState> {
    const doc = await this.request<ServerState>('GET', '/api/state');
    this.acceptState(doc);
    return doc;
  }

  /** Re-reads the whole document after a failure or a gap; errors are reported, not thrown. */
  async resync(): Promise<void> {
    if (this.resyncing) return;
    this.resyncing = true;
    try {
      await this.fetchState();
    } catch (err) {
      this.reportError(err, 'state', false);
    } finally {
      this.resyncing = false;
    }
  }

  private emitSent(msg: OutgoingMessage) {
    this.messageListeners.forEach((cb) => cb(msg));
  }

  private emit<K extends keyof BridgeEvents>(event: K, ...args: Parameters<BridgeEvents[K]>) {
    for (const cb of this.handlers[event] as Set<(...a: Parameters<BridgeEvents[K]>) => void>) cb(...args);
  }

  private setLink(link: BridgeLink) {
    if (this.link === link) return;
    this.link = link;
    this.emit('link', link);
  }

  private acceptState(doc: ServerState) {
    this.rev = doc.rev;
    this.emit('state', doc);
  }

  private enqueue(op: EditOp) {
    if (op.op === 'cc') this.queue = this.queue.filter((q) => !(q.op === 'cc' && q.cc === op.cc));
    if (op.op === 'volume') this.queue = this.queue.filter((q) => q.op !== 'volume');
    this.queue.push(op);
    if (!this.flushTimer) {
      this.flushTimer = setTimeout(() => void this.flush(), this.opts.coalesceMs ?? 33);
    }
  }

  private async postEdit(ops: EditOp[]): Promise<void> {
    try {
      const res = await this.request<{ applied: number; rev: number }>('POST', '/api/edit', {
        client_seq: ++this.clientSeq,
        ops,
      });
      this.rateLimitRetries = 0;
      if (typeof res.rev === 'number') this.rev = Math.max(this.rev, res.rev);
    } catch (err) {
      await this.handleEditFailure(err, ops);
    }
  }

  private async handleEditFailure(err: unknown, ops: EditOp[]): Promise<void> {
    const max = this.opts.maxRateLimitRetries ?? 4;
    if (err instanceof BridgeApiError && err.code === 'rate_limited' && this.rateLimitRetries < max) {
      this.rateLimitRetries++;
      const newer = new Set(this.queue.filter((q) => q.op === 'cc').map((q) => (q as { cc: number }).cc));
      const keep = ops.filter((o) => !(o.op === 'cc' && newer.has(o.cc)));
      this.queue = [...keep, ...this.queue];
      this.reportError(err, 'edit', false);
      if (!this.flushTimer) {
        this.flushTimer = setTimeout(() => void this.flush(), 500 * this.rateLimitRetries);
      }
      return;
    }
    this.rateLimitRetries = 0;
    this.reportError(err, 'edit', true);
    if (err instanceof BridgeApiError && err.code === 'unauthorized') return;
    await this.resync();
  }

  /** Tells the page about a failure of something the caller started (a file download, say). */
  reportError(err: unknown, context: BridgeErrorContext, lost: boolean) {
    const api = err instanceof BridgeApiError ? err : new BridgeApiError('network', 0, 'The server did not answer.');
    this.emit('error', {
      code: api.code,
      status: api.status,
      message: api.message,
      maybeApplied: api.maybeApplied,
      lost,
      context,
    });
    if (api.code === 'unauthorized') this.setLink('closed');
  }

  /** Sends a request and returns the response if it was a success; a failure becomes a BridgeApiError. */
  private async fetchOk(method: 'GET' | 'POST', path: string, body?: unknown, raw?: Blob): Promise<Response> {
    const doFetch = this.opts.fetch ?? globalThis.fetch.bind(globalThis);
    const headers: Record<string, string> = { 'X-Nanocore-Token': this.token ?? '' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (raw !== undefined) headers['Content-Type'] = 'application/octet-stream';
    let res: Response;
    try {
      res = await doFetch(path, { method, headers, body: raw ?? (body === undefined ? undefined : JSON.stringify(body)) });
    } catch {
      throw new BridgeApiError('network', 0, 'The server did not answer.');
    }
    if (!res.ok) {
      let json: unknown = null;
      try {
        json = await res.json();
      } catch {
        json = null;
      }
      const err = (json as { error?: { code?: string; message?: string; maybe_applied?: boolean } } | null)?.error;
      throw new BridgeApiError(
        err?.code ?? `http_${res.status}`,
        res.status,
        err?.message ?? `Request failed with status ${res.status}`,
        err?.maybe_applied === true,
      );
    }
    return res;
  }

  private async request<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
    const res = await this.fetchOk(method, path, body);
    try {
      return (await res.json()) as T;
    } catch {
      return null as T;
    }
  }

  private openSocket() {
    const Ctor = this.opts.WebSocket ?? (globalThis.WebSocket as unknown as SocketCtor);
    const ws = new Ctor(this.opts.wsUrl ?? defaultWsUrl());
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: 'auth', token: this.token }));
    ws.onmessage = (ev) => this.onSocketMessage(String(ev.data));
    ws.onerror = () => {};
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (ev.code === 4401) {
        this.reportError(new BridgeApiError('unauthorized', 401, 'The server rejected the token.'), 'socket', false);
        return;
      }
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect() {
    this.setLink('reconnecting');
    const backoff = this.opts.backoffMs ?? [500, 1000, 2000, 4000, 8000, 15000];
    const delay = backoff[Math.min(this.reconnectAttempt, backoff.length - 1)];
    this.reconnectAttempt++;
    const generation = this.generation;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (generation === this.generation) this.openSocket();
    }, delay);
  }

  private onSocketMessage(raw: string) {
    let msg: { type?: string; [k: string]: unknown };
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    switch (msg.type) {
      case 'state':
        this.reconnectAttempt = 0;
        this.setLink('connected');
        this.acceptState(msg.state as ServerState);
        break;
      case 'patch': {
        const rev = msg.rev as number;
        if (rev <= this.rev) break;
        if (rev !== this.rev + 1) {
          void this.resync();
          break;
        }
        this.rev = rev;
        this.emit('patch', rev, msg.ops as EditOp[]);
        break;
      }
      case 'autosave':
        this.emit('autosave', {
          state: msg.state as AutosaveInfo['state'],
          error: (msg.detail as string | null | undefined) ?? null,
        });
        if (msg.state === 'saved') void this.refreshAutosave();
        break;
      case 'connection':
        this.emit('connection', {
          connected: msg.connected === true,
          reason: (msg.reason as string) ?? null,
          released: msg.released === true,
        });
        break;
      case 'audio':
        this.emit('audio', { available: msg.available === true, on: msg.on === true });
        break;
      case 'error': {
        const e = (msg.error ?? {}) as { code?: string; message?: string; maybe_applied?: boolean };
        this.reportError(
          new BridgeApiError(e.code ?? 'unknown', 0, e.message ?? '', e.maybe_applied === true),
          'edit',
          true,
        );
        void this.resync();
        break;
      }
    }
  }

  private async refreshAutosave() {
    try {
      const doc = await this.request<ServerState>('GET', '/api/state');
      this.emit('autosave', doc.autosave);
    } catch {
      // The next state message carries it.
    }
  }
}

function defaultWsUrl(): string {
  const { protocol, host } = window.location;
  return `${protocol === 'https:' ? 'wss' : 'ws'}://${host}/ws`;
}

export const bridgeTransport = new BridgeTransport();
