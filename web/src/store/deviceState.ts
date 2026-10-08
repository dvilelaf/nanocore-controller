import { create } from 'zustand';
import { chainOrderToBlockIds } from '../midi/bridgeTransport';
import type {
  AssetsInfo,
  AutosaveInfo,
  BridgeErrorInfo,
  BridgeLink,
  BridgeTransport,
  EditOp,
  PresetListEntry,
  ServerState,
} from '../midi/bridgeTransport';
import { RUNTIME_BLOCK_ORDER, measuredType, translateEffects } from '../data/deviceMapping';
import type { RemoteSnapshot } from '../data/deviceMapping';
import { nanocoreSpec } from '../data/nanocoreSpec';

const AMP_MODELS = nanocoreSpec.blocks.find((b) => b.id === 'amp')!.types.length;
const CAB_MODELS = nanocoreSpec.blocks.find((b) => b.id === 'cab')!.types.length;

export type { AutosaveInfo, ServerState } from '../midi/bridgeTransport';

/** What the pedal (through the server) says, as opposed to what the editor sends. */
interface DeviceStore {
  doc: ServerState | null;
  /** True once a state document arrived: block values then come from the pedal. */
  hydrated: boolean;
  link: BridgeLink;
  pedalConnected: boolean;
  readOnly: boolean;
  preset: ServerState['preset'];
  autosave: AutosaveInfo | null;
  unaligned: string[];
  /** Parameter count of each block in the last document, to re-check a locally changed type. */
  paramCounts: Record<string, number>;
  assets: AssetsInfo | null;
  presets: PresetListEntry[];
  error: BridgeErrorInfo | null;

  /** Replaces the whole document and returns what the patch store should show. */
  applyState: (doc: ServerState) => RemoteSnapshot;
  /** Applies server operations. Returns null when one cannot be applied and a re-read is needed. */
  applyOps: (ops: EditOp[]) => RemoteSnapshot | null;
  /** The user chose another type for a block: recompute whether its layout matches the table. */
  retypeBlock: (blockId: string, typeId: number) => void;
  setLink: (link: BridgeLink) => void;
  setPedalConnected: (connected: boolean) => void;
  setAutosave: (patch: Partial<AutosaveInfo> & { state: AutosaveInfo['state'] }) => void;
  setAssets: (assets: AssetsInfo | null) => void;
  setPresets: (presets: PresetListEntry[]) => void;
  setError: (error: BridgeErrorInfo | null) => void;
  reset: () => void;
}

const initial = {
  doc: null,
  hydrated: false,
  link: 'idle' as BridgeLink,
  pedalConnected: false,
  readOnly: false,
  preset: null,
  autosave: null,
  unaligned: [] as string[],
  paramCounts: {} as Record<string, number>,
  assets: null,
  presets: [] as PresetListEntry[],
  error: null,
};

function countsOf(doc: ServerState): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const e of doc.live?.effects ?? []) {
    const id = RUNTIME_BLOCK_ORDER[e.index];
    if (id) counts[id] = e.params.length;
  }
  return counts;
}

function snapshotOf(doc: ServerState): RemoteSnapshot {
  const live = doc.live;
  if (!live) return { blocks: {}, unaligned: [], chainOrder: null };
  return translateEffects(live.effects, chainOrderToBlockIds(live.chain_order));
}

/** Pure: the document after the operations, or null if an operation is not understood. */
export function applyOpsToDoc(doc: ServerState, ops: EditOp[]): ServerState | null {
  if (!doc.live) return null;
  const live = { ...doc.live, effects: doc.live.effects.map((e) => ({ ...e, params: [...e.params] })) };
  let preset = doc.preset;
  for (const op of ops) {
    switch (op.op) {
      case 'name':
        if (preset) preset = { ...preset, name: op.name };
        break;
      case 'param': {
        const effect = live.effects.find((e) => e.index === op.effect);
        if (!effect || op.index < 0 || op.index >= effect.params.length) return null;
        effect.params[op.index] = op.value;
        break;
      }
      case 'enabled': {
        const effect = live.effects.find((e) => e.index === op.effect);
        if (!effect) return null;
        effect.enabled = op.enabled;
        break;
      }
      case 'variant': {
        const effect = live.effects.find((e) => e.index === op.effect);
        if (!effect) return null;
        effect.variant = op.variant;
        effect.params = [...op.params];
        break;
      }
      case 'volume':
        live.volume = op.value;
        break;
      case 'chain_order':
        live.chain_order = [...op.order];
        break;
      case 'amp':
      case 'ir':
        break;
      default:
        return null;
    }
  }
  return { ...doc, live, preset };
}

/** The dropdown list with the name of the active preset as the document says it (a live rename, or the stored one again). */
function withActiveName(presets: PresetListEntry[], active: ServerState['preset']): PresetListEntry[] {
  if (!active) return presets;
  return presets.map((p) => (p.slot === active.slot && p.name !== active.name ? { ...p, name: active.name } : p));
}

export const useDeviceStore = create<DeviceStore>((set, get) => ({
  ...initial,

  applyState: (doc) => {
    const snapshot = snapshotOf(doc);
    set((s) => ({
      doc,
      hydrated: !!doc.live,
      pedalConnected: doc.connected,
      readOnly: doc.read_only,
      preset: doc.preset,
      presets: withActiveName(s.presets, doc.preset),
      autosave: doc.autosave,
      unaligned: snapshot.unaligned,
      paramCounts: countsOf(doc),
    }));
    return snapshot;
  },

  applyOps: (ops) => {
    const doc = get().doc;
    if (!doc) return null;
    const next = applyOpsToDoc(doc, ops);
    if (!next) return null;
    const snapshot = snapshotOf(next);
    set((s) => ({
      doc: next,
      preset: next.preset,
      presets: withActiveName(s.presets, next.preset),
      unaligned: snapshot.unaligned,
      paramCounts: countsOf(next),
    }));
    return snapshot;
  },

  retypeBlock: (blockId, typeId) =>
    set((s) => {
      if (!s.hydrated) return {};
      const known = s.paramCounts[blockId];
      const rest = s.unaligned.filter((id) => id !== blockId);
      if (known === undefined) return { unaligned: rest };
      const measured = measuredType(blockId, typeId);
      return { unaligned: !measured || measured.param_count !== known ? [...rest, blockId] : rest };
    }),
  setLink: (link) => set({ link }),
  setPedalConnected: (pedalConnected) => set({ pedalConnected }),
  setAutosave: (patch) =>
    set((s) => ({
      autosave: {
        enabled: s.autosave?.enabled ?? true,
        last_saved_at: s.autosave?.last_saved_at ?? null,
        error: null,
        ...patch,
      },
    })),
  setAssets: (assets) => set({ assets }),
  setPresets: (presets) => set({ presets }),
  setError: (error) => set({ error }),
  reset: () => set({ ...initial }),
}));

/**
 * Connects a bridge transport to the stores. `applyRemote` must update the editor's patch
 * without sending anything back to the pedal.
 */
export function attachBridge(transport: BridgeTransport, applyRemote: (snapshot: RemoteSnapshot) => void): () => void {
  const device = useDeviceStore.getState;
  let lastSlot: number | null = null;

  const refreshAssets = () => {
    transport
      .fetchAssets()
      .then((assets) => {
        device().setAssets(assets);
        const blocks: RemoteSnapshot['blocks'] = {};
        if (assets.amp && assets.amp.slot >= 0 && assets.amp.slot < AMP_MODELS) blocks.amp = { typeId: assets.amp.slot };
        if (assets.ir && assets.ir.slot >= 0 && assets.ir.slot < CAB_MODELS) blocks.cab = { typeId: assets.ir.slot };
        applyRemote({ blocks, unaligned: device().unaligned, chainOrder: null });
      })
      .catch(() => {});
  };

  const offs = [
    transport.on('link', (link) => device().setLink(link)),
    transport.on('state', (doc) => {
      applyRemote(device().applyState(doc));
      if (device().error?.context === 'state') device().setError(null);
      const slot = doc.preset?.slot ?? null;
      if (slot !== lastSlot && doc.connected) {
        lastSlot = slot;
        refreshAssets();
      }
    }),
    transport.on('patch', (_rev, ops) => {
      const snapshot = device().applyOps(ops);
      if (!snapshot) {
        void transport.resync();
        return;
      }
      applyRemote(snapshot);
      if (ops.some((o) => o.op === 'amp' || o.op === 'ir')) refreshAssets();
    }),
    transport.on('autosave', (a) => device().setAutosave(a)),
    transport.on('connection', ({ connected }) => device().setPedalConnected(connected)),
    transport.on('error', (e) => device().setError(e)),
  ];
  return () => offs.forEach((off) => off());
}
