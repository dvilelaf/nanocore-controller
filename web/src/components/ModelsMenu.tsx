import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { BridgeApiError, MAX_MODEL_BYTES, bridgeTransport } from '../midi/bridgeTransport';
import type { ModelEntry, ModelKind, ModelList } from '../midi/bridgeTransport';
import { useDeviceStore } from '../store/deviceState';
import { usePatchStore } from '../store/patchStore';

const TABS: ModelKind[] = ['amp', 'ir'];
const ACCEPT: Record<ModelKind, string> = { amp: '.bin,.ead,.eadl', ir: '.wav,.bin' };

const iconProps = {
  width: 18,
  height: 18,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
};

function DownloadIcon() {
  return (
    <svg {...iconProps}>
      <path d="M12 3v12" />
      <path d="m7 10 5 5 5-5" />
      <path d="M5 21h14" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg {...iconProps}>
      <path d="M12 15V3" />
      <path d="m7 8 5-5 5 5" />
      <path d="M5 21h14" />
    </svg>
  );
}

function LayersIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m12 2 10 5-10 5L2 7z" />
      <path d="m2 12 10 5 10-5" />
      <path d="m2 17 10 5 10-5" />
    </svg>
  );
}

function saveAsFile(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** A refusal carries the server's message; anything else is a plain network failure. */
const messageOf = (err: unknown) => (err instanceof BridgeApiError ? err.message : 'The server did not answer.');

/**
 * The amplifier and cabinet (IR) models stored in the pedal: list them, save one to a file, or replace one with a
 * file. Writing a slot is not an edit of the preset: nothing becomes unsaved. The server keeps the old content of
 * a replaced slot in a backup on its computer.
 */
function ModelsPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const { t } = useTranslation();
  const readOnly = useDeviceStore((s) => s.readOnly);
  const pedalConnected = useDeviceStore((s) => s.pedalConnected);
  const [models, setModels] = useState<ModelList | null>(null);
  const [tab, setTab] = useState<ModelKind>('amp');
  const [problem, setProblem] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const alive = useRef(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const target = useRef<{ kind: ModelKind; entry: ModelEntry } | null>(null);
  const tabRefs = useRef<Record<ModelKind, HTMLButtonElement | null>>({ amp: null, ir: null });
  const ids = { amp: useId(), ir: useId(), panel: useId() };

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await bridgeTransport.fetchModels();
      if (alive.current) setModels(next);
    } catch {
      if (alive.current) setProblem(t('models.readError', 'The models could not be read from the pedal.'));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (pedalConnected) void load();
  }, [pedalConnected, load]);

  // The list scrolls inside the panel: show the slot in use when the list or the tab changes.
  const activeRow = useRef<HTMLLIElement>(null);
  useEffect(() => {
    activeRow.current?.scrollIntoView?.({ block: 'nearest' });
  }, [tab, models]);

  const download = async (kind: ModelKind, entry: ModelEntry) => {
    setProblem(null);
    setWorking(t('models.reading', 'Reading slot {{slot}}…', { slot: entry.slot }));
    try {
      const { filename, blob } = await bridgeTransport.downloadModel(kind, entry.slot);
      saveAsFile(filename, blob);
    } catch (err) {
      if (alive.current) setProblem(t('models.slotError', 'Slot {{slot}}: {{message}}', { slot: entry.slot, message: messageOf(err) }));
    } finally {
      if (alive.current) setWorking(null);
    }
  };

  const chooseFile = (kind: ModelKind, entry: ModelEntry) => {
    target.current = { kind, entry };
    if (fileInput.current) {
      fileInput.current.accept = ACCEPT[kind];
      fileInput.current.click();
    }
  };

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = ''; // so that choosing the same file again still counts as a change
    const chosen = target.current;
    target.current = null;
    if (!file || !chosen) return;
    const { kind, entry } = chosen;
    setProblem(null);
    if (file.size > MAX_MODEL_BYTES) {
      setProblem(t('models.tooLarge', 'The file is larger than 4 MiB.'));
      return;
    }
    const ok = window.confirm(
      t(
        'models.confirm',
        "Replace '{{name}}' in slot {{slot}}? The old content is kept in a backup on the computer running the server.",
        { name: entry.name, slot: entry.slot },
      ),
    );
    if (!ok) return;
    setWorking(t('models.writing', 'Writing slot {{slot}}…', { slot: entry.slot }));
    try {
      await bridgeTransport.uploadModel(kind, entry.slot, file);
    } catch (err) {
      if (alive.current) setProblem(t('models.slotError', 'Slot {{slot}}: {{message}}', { slot: entry.slot, message: messageOf(err) }));
      // A refusal before any write (400, 403, 413) changed nothing; anything else may have, so look again.
      const untouched = err instanceof BridgeApiError && [400, 403, 413].includes(err.status);
      if (!untouched && alive.current) await load();
      if (alive.current) setWorking(null);
      return;
    }
    try {
      // The block cards show the name of the active amplifier and IR: take a renamed one.
      useDeviceStore.getState().setAssets(await bridgeTransport.fetchAssets());
    } catch {
      // the names refresh at the next preset change
    }
    if (alive.current) await load();
    if (alive.current) setWorking(null);
  };

  const onTabKey = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const at = TABS.indexOf(tab);
    let next: number | null = null;
    if (event.key === 'ArrowRight') next = (at + 1) % TABS.length;
    else if (event.key === 'ArrowLeft') next = (at + TABS.length - 1) % TABS.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = TABS.length - 1;
    if (next === null) return;
    event.preventDefault();
    setTab(TABS[next]);
    tabRefs.current[TABS[next]]?.focus();
  };

  const tabLabel: Record<ModelKind, string> = {
    amp: t('models.amps', 'Amplifiers'),
    ir: t('models.irs', 'Cabinets (IR)'),
  };
  const rows = models?.[tab] ?? [];
  const busy = working !== null;

  return (
    <div id={id} className="settings-panel models-panel" role="dialog" aria-label={t('models.title', 'Amplifier and cabinet models')} aria-busy={busy || loading}>
      <div className="settings-panel__head">
        <h2 className="settings-panel__title">{t('models.title', 'Amplifier and cabinet models')}</h2>
        <button type="button" className="btn btn--ghost settings-panel__close" aria-label={t('models.close', 'Close')} onClick={onClose}>
          <span aria-hidden>×</span>
        </button>
      </div>

      <div className="models-tabs" role="tablist" aria-label={t('models.kinds', 'Kind of model')}>
        {TABS.map((kind) => (
          <button
            key={kind}
            ref={(el) => {
              tabRefs.current[kind] = el;
            }}
            id={ids[kind]}
            type="button"
            role="tab"
            className={`models-tab ${tab === kind ? 'models-tab--on' : ''}`}
            aria-selected={tab === kind}
            aria-controls={ids.panel}
            tabIndex={tab === kind ? 0 : -1}
            onClick={() => setTab(kind)}
            onKeyDown={onTabKey}
          >
            {tabLabel[kind]}
          </button>
        ))}
      </div>

      {tab === 'amp' && (
        <>
          <p className="settings-panel__note">
            {models?.ead
              ? t('models.ampNoteEad', "Models read from a pedal can be written here, and Livtra's .ead files through your decryptor.")
              : t(
                  'models.ampNote',
                  "Models read from a pedal can be written here. Livtra's encrypted .ead files need a decryptor that you provide (docs/ead-decryptor.md); none is configured on this server.",
                )}
          </p>
          <p className="settings-panel__note">{t('models.driveNote', 'Amplifiers 30 to 37 are drive models')}</p>
        </>
      )}
      {readOnly && <p className="settings-panel__note">{t('models.readOnly', 'The server is read-only: nothing can be written.')}</p>}
      {!pedalConnected && <p className="settings-panel__note">{t('settings.disconnected', 'The pedal is not connected.')}</p>}

      <div id={ids.panel} role="tabpanel" aria-labelledby={ids[tab]}>
        {loading && !models && <p className="settings-panel__note">{t('models.loading', 'Reading the pedal…')}</p>}
        {models && (
          <ul className="models-list">
            {rows.map((entry) => (
              <li key={entry.slot} ref={entry.active ? activeRow : undefined} className={`models-row ${entry.active ? 'models-row--active' : ''}`}>
                <span className="models-row__slot">{entry.slot}</span>
                <span className="models-row__name" title={entry.name}>{entry.name}</span>
                {entry.active ? <span className="models-row__badge">{t('models.active', 'Active')}</span> : <span />}
                <button
                  type="button"
                  className="btn models-row__icon"
                  aria-label={t('models.download', 'Download slot {{slot}} ({{name}})', { slot: entry.slot, name: entry.name })}
                  title={t('models.downloadTitle', 'Save this model to a file on your computer')}
                  disabled={!pedalConnected || busy}
                  onClick={() => void download(tab, entry)}
                >
                  <DownloadIcon />
                </button>
                <button
                  type="button"
                  className="btn models-row__icon"
                  aria-label={t('models.upload', 'Upload to slot {{slot}} ({{name}})', { slot: entry.slot, name: entry.name })}
                  title={
                    tab === 'amp'
                      ? t('models.uploadAmpTitle', 'Replace this model with a .bin file read from a pedal, or an .ead file')
                      : t('models.uploadIrTitle', 'Replace this cabinet with a .wav or .bin file')
                  }
                  disabled={readOnly || !pedalConnected || busy}
                  onClick={() => chooseFile(tab, entry)}
                >
                  <UploadIcon />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <input ref={fileInput} type="file" hidden tabIndex={-1} aria-hidden onChange={(e) => void upload(e)} />

      {working && (
        <p className="settings-panel__note" role="status">
          {working}
        </p>
      )}
      {problem && (
        <p className="settings-panel__error" role="alert">
          {problem}
        </p>
      )}
    </div>
  );
}

/** Floating layers button beside the settings gear; it opens the models panel at the bottom left of the screen. */
export function ModelsMenu({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t } = useTranslation();
  const isBridge = usePatchStore((s) => s.connection.transportKind === 'bridge');
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onOpenChange(false);
        button.current?.focus();
      }
    };
    const onPointer = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) onOpenChange(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onPointer);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onPointer);
    };
  }, [open, onOpenChange]);

  if (!isBridge) return null;
  return (
    <div className="settings-menu" ref={root}>
      <button
        type="button"
        ref={button}
        className="btn fab models-fab"
        aria-label={t('models.toggle', 'Models')}
        title={t('models.toggleTitle', 'Amplifier and cabinet models stored in the pedal: save them to files or replace them')}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => onOpenChange(!open)}
      >
        <LayersIcon />
      </button>
      {open && <ModelsPanel id={panelId} onClose={() => onOpenChange(false)} />}
    </div>
  );
}
