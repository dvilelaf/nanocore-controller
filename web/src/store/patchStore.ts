import { create } from 'zustand';
import { nanocoreSpec } from '../data/nanocoreSpec';
import { BleMidiTransport, SimulatorTransport, WebMidiTransport, bridgeTransport } from '../midi';
import type { MidiPortInfo, MidiTransport, OutgoingMessage } from '../midi/types';
import { enumIndexToCC, onOffToCC, realToCC } from '../midi/scaling';
import { CHAIN_ORDER_BLOCK_IDS, buildBlockTypeSysEx, buildChainOrderSysEx } from '../midi/sysex';
import {
  activeParams,
  buildBlockDefault,
  buildDefaultPatch,
  findBlock,
  findParamSpec,
  findType,
  remapParamsForType,
  validatePatch,
} from './patchDefaults';
import { loadPresets, savePresets } from './localPresetStorage';
import type { BlockPatchState, PatchState, PresetEntry } from './patchTypes';
import type { RemoteSnapshot } from '../data/deviceMapping';
import { attachBridge, useDeviceStore } from './deviceState';

const MAX_LOG_LENGTH = 300;

/** A control edited locally this recently ignores values coming back from the pedal. */
const ECHO_WINDOW_MS = 500;
let clock: () => number = () => Date.now();
const lastParamEdit = new Map<string, number>();

/** Test hook: replaces the clock used for echo suppression and forgets earlier edits. */
export function setEditClock(fn: () => number): void {
  clock = fn;
  lastParamEdit.clear();
}

const webMidiTransport = new WebMidiTransport();
const simulatorTransport = new SimulatorTransport();
const bleMidiTransport = new BleMidiTransport();

function transportFor(kind: TransportKind): MidiTransport {
  if (kind === 'webmidi') return webMidiTransport;
  if (kind === 'bluetooth') return bleMidiTransport;
  if (kind === 'bridge') return bridgeTransport;
  return simulatorTransport;
}

let unsubPorts: (() => void) | null = null;
let unsubMessages: (() => void) | null = null;
let unsubBridge: (() => void) | null = null;
let initGeneration = 0;

export type TransportKind = 'webmidi' | 'simulator' | 'bluetooth' | 'bridge';

interface ConnectionState {
  transportKind: TransportKind;
  outputId: string | null;
  channel: number; // 1-16, transmit channel
  outputs: MidiPortInfo[];
  ready: boolean;
  initializing: boolean;
  error: string | null;
}

interface PatchStore {
  patch: PatchState;
  /** Effect chain order (block ids). Kept separately from `patch`/presets for now — SysEx-only,
   * not covered by the manual, and not yet round-tripped through preset save/export. See
   * setChainOrder below and docs/MIDI_MAPPING_NOTES.md. */
  chainOrder: string[];
  connection: ConnectionState;
  log: OutgoingMessage[];
  presets: PresetEntry[];
  activePresetId: string | null;
  activePresetDirty: boolean;

  // Connection
  initTransport: (kind: TransportKind) => Promise<void>;
  refreshOutputs: () => void;
  setOutput: (id: string) => void;
  setChannel: (ch: number) => void;
  clearLog: () => void;
  disconnectBluetooth: () => void;

  // Patch editing
  setBlockOn: (blockId: string, on: boolean) => void;
  setBlockType: (blockId: string, typeId: number) => void;
  setParam: (blockId: string, paramId: string, value: number) => void;
  loadPatch: (patch: PatchState) => void;
  resetPatch: () => void;
  /** Moves a block one step left/right in the effect chain and sends the resulting order via
   * SysEx (fieldId 0x05 — see midi/sysex.ts). No-ops at either end of the chain. */
  moveBlockInChain: (blockId: string, direction: 'left' | 'right') => void;
  /** Moves a block to an arbitrary position (drag and drop) and sends the new order once. */
  moveBlockTo: (blockId: string, toIndex: number) => void;
  /** Shows state reported by the pedal. Never sends anything to the transport. */
  applyRemoteState: (snapshot: RemoteSnapshot) => void;

  // Device-level actions
  sendFullPatch: () => void;
  recallProgram: (program: number) => void;
  stepPreset: (direction: 'prev' | 'next') => void;
  setTuner: (on: boolean) => void;

  // Local preset library
  savePresetLocal: (name: string) => void;
  renamePreset: (id: string, name: string) => void;
  updateActivePreset: () => void;
  deletePreset: (id: string) => void;
  applyPreset: (id: string) => void;
  exportPresets: () => string;
  importPresets: (json: string) => { ok: boolean; error?: string };

  // Single-patch export/import (the current, possibly-unsaved patch — distinct from the
  // saved preset library above).
  exportCurrentPatch: () => string;
  importPatch: (json: string) => { ok: boolean; error?: string };
}

function currentOutputId(state: PatchStore): string | null {
  return state.connection.outputId;
}

export const usePatchStore = create<PatchStore>((set, get) => ({
  patch: buildDefaultPatch(),
  chainOrder: nanocoreSpec.blocks.map((b) => b.id),
  connection: {
    transportKind: 'simulator',
    outputId: null,
    channel: 1,
    outputs: [],
    ready: false,
    initializing: false,
    error: null,
  },
  log: [],
  presets: loadPresets(),
  activePresetId: null,
  activePresetDirty: false,

  initTransport: async (kind) => {
    // Guard against overlapping calls (React StrictMode's double-effect in dev, or the user
    // rapidly switching transports): only the most recent call is allowed to (un)subscribe.
    const myGeneration = ++initGeneration;
    unsubPorts?.();
    unsubMessages?.();
    unsubPorts = null;
    unsubMessages = null;
    unsubBridge?.();
    unsubBridge = null;
    if (kind !== 'bridge') {
      bridgeTransport.close();
      useDeviceStore.getState().reset();
    }
    set((s) => ({ connection: { ...s.connection, transportKind: kind, initializing: true, error: null } }));

    const transport = transportFor(kind);
    if (kind === 'bridge') {
      unsubBridge = attachBridge(bridgeTransport, (snapshot) => get().applyRemoteState(snapshot));
    }
    try {
      await transport.init();
      if (myGeneration !== initGeneration) return; // superseded by a newer initTransport call
      const outputs = transport.listOutputs();
      set((s) => ({
        connection: {
          ...s.connection,
          ready: true,
          initializing: false,
          outputs,
          outputId: outputs[0]?.id ?? null,
        },
      }));
      unsubPorts = transport.onPortsChanged(() => {
        set((s) => ({ connection: { ...s.connection, outputs: transport.listOutputs() } }));
      });
      unsubMessages = transport.onMessageSent((msg) => {
        set((s) => ({ log: [msg, ...s.log].slice(0, MAX_LOG_LENGTH) }));
      });
    } catch (err) {
      if (myGeneration !== initGeneration) return;
      set((s) => ({
        connection: {
          ...s.connection,
          ready: false,
          initializing: false,
          error: err instanceof Error ? err.message : String(err),
        },
      }));
    }
  },

  refreshOutputs: () => {
    const transport = transportFor(get().connection.transportKind);
    set((s) => ({ connection: { ...s.connection, outputs: transport.listOutputs() } }));
  },

  setOutput: (id) => set((s) => ({ connection: { ...s.connection, outputId: id } })),
  setChannel: (ch) => set((s) => ({ connection: { ...s.connection, channel: Math.min(16, Math.max(1, ch)) } })),
  clearLog: () => set({ log: [] }),

  disconnectBluetooth: () => {
    bleMidiTransport.disconnect();
    if (get().connection.transportKind === 'bluetooth') {
      set((s) => ({
        connection: { ...s.connection, outputs: [], outputId: null, ready: false },
      }));
    }
  },

  setBlockOn: (blockId, on) => {
    const block = findBlock(blockId);
    set((s) => ({
      patch: { ...s.patch, [blockId]: { ...s.patch[blockId], on } },
      activePresetDirty: true,
    }));
    const state = get();
    const outputId = currentOutputId(state);
    if (!outputId) return;
    const transport = transportFor(state.connection.transportKind);
    transport.sendCC(
      outputId,
      state.connection.channel,
      block.onOffCC,
      onOffToCC(on),
      `${block.name} ${on ? 'ON' : 'OFF'}`,
    );
  },

  setBlockType: (blockId, typeId) => {
    useDeviceStore.getState().retypeBlock(blockId, typeId);
    const block = findBlock(blockId);
    const type = findType(block, typeId);
    set((s) => {
      const prev = s.patch[blockId];
      return {
        patch: {
          ...s.patch,
          [blockId]: { ...prev, typeId, params: remapParamsForType(block, type, prev.params) },
        },
        activePresetDirty: true,
      };
    });
    const state = get();
    const outputId = currentOutputId(state);
    if (!outputId) return;
    const transport = transportFor(state.connection.transportKind);
    if (block.sysexTypeField !== undefined) {
      // Confirmed on real hardware: typeCC has no effect for this block — SysEx is the only way.
      // See midi/sysex.ts and docs/MIDI_MAPPING_NOTES.md.
      transport.sendSysEx(
        outputId,
        buildBlockTypeSysEx(block.sysexTypeField, typeId),
        `${block.name} type -> ${type.name}`,
      );
    } else {
      transport.sendCC(outputId, state.connection.channel, block.typeCC, typeId, `${block.name} type -> ${type.name}`);
    }
  },

  setParam: (blockId, paramId, value) => {
    lastParamEdit.set(`${blockId}:${paramId}`, clock());
    set((s) => ({
      patch: {
        ...s.patch,
        [blockId]: { ...s.patch[blockId], params: { ...s.patch[blockId].params, [paramId]: value } },
      },
      activePresetDirty: true,
    }));
    const state = get();
    const outputId = currentOutputId(state);
    if (!outputId) return;
    const block = findBlock(blockId);
    const blockState = state.patch[blockId];
    const spec = findParamSpec(block, blockState.typeId, paramId);
    const raw = spec.kind === 'range' ? realToCC(spec.min, spec.max, value) : enumIndexToCC(value, spec.options.length);
    const transport = transportFor(state.connection.transportKind);
    const displayValue = spec.kind === 'range' ? value.toFixed(spec.decimals ?? 0) : spec.options[value];
    transport.sendCC(
      outputId,
      state.connection.channel,
      spec.cc,
      raw,
      `${block.name} ${findType(block, blockState.typeId).name}: ${spec.label} = ${displayValue}${
        spec.kind === 'range' && spec.unit ? spec.unit : ''
      }`,
    );
  },

  loadPatch: (patch) => set({ patch, activePresetDirty: true }),

  resetPatch: () =>
    set({
      patch: buildDefaultPatch(),
      chainOrder: nanocoreSpec.blocks.map((b) => b.id),
      activePresetId: null,
      activePresetDirty: false,
    }),

  moveBlockInChain: (blockId, direction) => {
    const i = get().chainOrder.indexOf(blockId);
    if (i === -1) return;
    get().moveBlockTo(blockId, direction === 'left' ? i - 1 : i + 1);
  },

  moveBlockTo: (blockId, toIndex) => {
    const state = get();
    const order = state.chainOrder;
    const i = order.indexOf(blockId);
    if (i === -1 || toIndex < 0 || toIndex >= order.length || toIndex === i) return;

    const newOrder = [...order];
    newOrder.splice(i, 1);
    newOrder.splice(toIndex, 0, blockId);
    set({ chainOrder: newOrder });

    const outputId = currentOutputId(state);
    if (!outputId) return;
    const transport = transportFor(state.connection.transportKind);
    const ids = newOrder.map((id) => CHAIN_ORDER_BLOCK_IDS[id]);
    transport.sendSysEx(
      outputId,
      buildChainOrderSysEx(ids),
      `Chain order -> ${newOrder.join(' > ')}`,
    );
  },

  applyRemoteState: ({ blocks, chainOrder }) =>
    set((s) => {
      const patch: PatchState = { ...s.patch };
      const now = clock();
      for (const [blockId, remote] of Object.entries(blocks)) {
        const prev = patch[blockId];
        if (!prev) continue;
        const block = findBlock(blockId);
        const typeId = remote.typeId ?? prev.typeId;
        const base =
          typeId === prev.typeId ? prev.params : remapParamsForType(block, findType(block, typeId), prev.params);
        const incoming: Record<string, number> = {};
        for (const [id, v] of Object.entries(remote.params ?? {})) {
          const edited = lastParamEdit.get(`${blockId}:${id}`);
          if (edited === undefined || now - edited >= ECHO_WINDOW_MS) incoming[id] = v;
        }
        patch[blockId] = { on: remote.on ?? prev.on, typeId, params: { ...base, ...incoming } };
      }
      return chainOrder ? { patch, chainOrder } : { patch };
    }),

  sendFullPatch: () => {
    const state = get();
    const outputId = currentOutputId(state);
    if (!outputId) return;
    const transport = transportFor(state.connection.transportKind);
    const { channel } = state.connection;

    for (const block of nanocoreSpec.blocks) {
      const blockState: BlockPatchState = state.patch[block.id] ?? buildBlockDefault(block);
      transport.sendCC(outputId, channel, block.onOffCC, onOffToCC(blockState.on), `${block.name} ${blockState.on ? 'ON' : 'OFF'}`);
      const typeName = findType(block, blockState.typeId).name;
      if (block.sysexTypeField !== undefined) {
        transport.sendSysEx(
          outputId,
          buildBlockTypeSysEx(block.sysexTypeField, blockState.typeId),
          `${block.name} type -> ${typeName}`,
        );
      } else {
        transport.sendCC(outputId, channel, block.typeCC, blockState.typeId, `${block.name} type -> ${typeName}`);
      }
      for (const spec of activeParams(block, blockState.typeId)) {
        const value = blockState.params[spec.id] ?? (spec.kind === 'range' ? spec.min : 0);
        const raw = spec.kind === 'range' ? realToCC(spec.min, spec.max, value) : enumIndexToCC(value, spec.options.length);
        transport.sendCC(outputId, channel, spec.cc, raw, `${block.name}: ${spec.label}`);
      }
    }
  },

  recallProgram: (program) => {
    const state = get();
    const outputId = currentOutputId(state);
    if (!outputId) return;
    const transport = transportFor(state.connection.transportKind);
    transport.sendProgramChange(outputId, state.connection.channel, program, `Recall device preset ${program}`);
  },

  stepPreset: (direction) => {
    const state = get();
    const outputId = currentOutputId(state);
    if (!outputId) return;
    const transport = transportFor(state.connection.transportKind);
    const cc = direction === 'prev' ? 81 : 82;
    const label = direction === 'prev' ? 'Previous preset' : 'Next preset';
    transport.sendCC(outputId, state.connection.channel, cc, 127, label);
    // Momentary control: release shortly after, per MIDI guide ("64-127 triggers").
    window.setTimeout(() => transport.sendCC(outputId, state.connection.channel, cc, 0, `${label} (release)`), 80);
  },

  setTuner: (on) => {
    const state = get();
    const outputId = currentOutputId(state);
    if (!outputId) return;
    const transport = transportFor(state.connection.transportKind);
    transport.sendCC(outputId, state.connection.channel, 80, onOffToCC(on), `Tuner ${on ? 'ON' : 'OFF'}`);
  },

  savePresetLocal: (name) => {
    const state = get();
    const entry: PresetEntry = {
      id: crypto.randomUUID(),
      name: name.trim() || 'Untitled preset',
      patch: state.patch,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    const presets = [...state.presets, entry];
    savePresets(presets);
    set({ presets, activePresetId: entry.id, activePresetDirty: false });
  },

  renamePreset: (id, name) => {
    const presets = get().presets.map((p) => (p.id === id ? { ...p, name: name.trim() || p.name } : p));
    savePresets(presets);
    set({ presets });
  },

  updateActivePreset: () => {
    const state = get();
    if (!state.activePresetId) return;
    const presets = state.presets.map((p) =>
      p.id === state.activePresetId ? { ...p, patch: state.patch, updatedAt: Date.now() } : p,
    );
    savePresets(presets);
    set({ presets, activePresetDirty: false });
  },

  deletePreset: (id) => {
    const presets = get().presets.filter((p) => p.id !== id);
    savePresets(presets);
    set((s) => ({
      presets,
      activePresetId: s.activePresetId === id ? null : s.activePresetId,
    }));
  },

  applyPreset: (id) => {
    const preset = get().presets.find((p) => p.id === id);
    if (!preset) return;
    set({ patch: preset.patch, activePresetId: id, activePresetDirty: false });
  },

  exportPresets: () => JSON.stringify(get().presets, null, 2),

  importPresets: (json) => {
    try {
      const parsed = JSON.parse(json);
      if (!Array.isArray(parsed)) return { ok: false, error: 'Expected a JSON array of presets.' };
      // Re-key ids to avoid collisions with existing presets.
      const incoming: PresetEntry[] = parsed.map((p: PresetEntry) => ({
        ...p,
        id: crypto.randomUUID(),
      }));
      const presets = [...get().presets, ...incoming];
      savePresets(presets);
      set({ presets });
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : String(err) };
    }
  },

  exportCurrentPatch: () => JSON.stringify(get().patch, null, 2),

  importPatch: (json) => {
    try {
      const parsed = JSON.parse(json);
      const result = validatePatch(parsed);
      if (!result.ok) return result;
      set({ patch: result.patch, activePresetId: null, activePresetDirty: true });
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : String(err) };
    }
  },
}));
