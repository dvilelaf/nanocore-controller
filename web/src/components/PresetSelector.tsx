import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { bridgeTransport } from '../midi/bridgeTransport';
import { useDeviceStore } from '../store/deviceState';
import { AutosaveBadge } from './ConnectionStatus';
import { usePresetFiles } from './usePresetFiles';

/** Loads the pedal's preset list into the store, and again whenever the pedal reconnects. */
export function usePresetList(): { failed: boolean } {
  const setPresets = useDeviceStore((s) => s.setPresets);
  const pedalConnected = useDeviceStore((s) => s.pedalConnected);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    bridgeTransport
      .fetchPresets()
      .then((list) => {
        if (cancelled) return;
        setFailed(false);
        setPresets(list);
      })
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, [setPresets, pedalConnected]);

  return { failed };
}

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

function SaveIcon() {
  return (
    <svg {...iconProps}>
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
      <path d="M17 21v-8H7v8" />
      <path d="M7 3v5h8" />
    </svg>
  );
}

function RestoreIcon() {
  return (
    <svg {...iconProps}>
      <path d="M3 12a9 9 0 1 0 3-6.7" />
      <path d="M3 4v5h5" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg {...iconProps}>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
    </svg>
  );
}

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

function CheckIcon() {
  return (
    <svg {...iconProps}>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

function CrossIcon() {
  return (
    <svg {...iconProps}>
      <path d="M18 6 6 18" />
      <path d="M6 6l12 12" />
    </svg>
  );
}

const MAX_NAME_LENGTH = 8;
/** The pedal shows names of up to 8 printable ASCII characters. */
const toPedalName = (text: string) => text.replace(/[^\x20-\x7e]/g, '').slice(0, MAX_NAME_LENGTH);

/** Picks the active preset of the pedal. Names come from the device and are rendered as plain text. */
export function PresetSelector() {
  const { t } = useTranslation();
  usePresetList();
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState('');
  const nameInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const files = usePresetFiles();
  const presets = useDeviceStore((s) => s.presets);
  const active = useDeviceStore((s) => s.preset);
  const readOnly = useDeviceStore((s) => s.readOnly);
  const pedalConnected = useDeviceStore((s) => s.pedalConnected);
  const unsaved = useDeviceStore((s) => s.autosave?.state === 'dirty' || s.autosave?.state === 'error');
  const saving = useDeviceStore((s) => s.autosave?.state === 'saving');

  const options = presets.length > 0 || !active ? presets : [{ slot: active.slot, display_number: active.display_number, name: active.name }];
  const locked = readOnly || !pedalConnected;
  const label = (p: { display_number: number; name: string }) => `${p.display_number} · ${p.name}`;
  const go = (displayNumber: number) => {
    if (displayNumber === active?.display_number) return;
    if (unsaved && !window.confirm(t('preset.selector.discardConfirm', 'This preset has unsaved changes. Switching will discard them. Continue?'))) {
      return;
    }
    void bridgeTransport.recallPreset(displayNumber, unsaved);
  };
  const canAct = !locked && unsaved && !saving;
  const volume = useDeviceStore((s) => s.doc?.live?.volume ?? null);
  const changeVolume = (value: number) => {
    useDeviceStore.getState().applyOps([{ op: 'volume', value }]);
    bridgeTransport.setPresetVolume(value);
  };

  const activeSlot = active?.slot ?? null;
  useEffect(() => {
    setRenaming(false); // the pedal moved to another preset: the name being typed belongs to the old one
  }, [activeSlot]);
  useEffect(() => {
    if (renaming) nameInput.current?.select();
  }, [renaming]);

  const startRename = () => {
    if (!active) return;
    setDraft(toPedalName(active.name));
    setRenaming(true);
  };
  const newName = draft.replace(/ +$/, '');
  const applyName = () => {
    if (!newName) return;
    setRenaming(false);
    if (!active || newName === active.name) return;
    // Show it at once (the server's own patch is not sent back to the page that made the edit).
    useDeviceStore.getState().applyOps([{ op: 'name', name: newName }]);
    void bridgeTransport.renamePreset(newName);
  };
  const onNameKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      applyName();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      setRenaming(false);
    }
  };

  return (
    <div className="preset-selector">
      {renaming ? (
        <label className="preset-selector__field">
          <span className="preset-selector__label">{t('preset.rename.label', 'Preset name')}</span>
          <input
            ref={nameInput}
            type="text"
            className="preset-selector__select preset-selector__name"
            value={draft}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setDraft(toPedalName(e.target.value))}
            onKeyDown={onNameKey}
          />
        </label>
      ) : (
        <label className="preset-selector__field">
          <span className="preset-selector__label">{t('preset.selector.label', 'Preset')}</span>
          <select
            className="preset-selector__select"
            value={active ? active.display_number : ''}
            disabled={locked}
            onChange={(e) => go(Number(e.target.value))}
          >
            {options.map((p) => (
              <option key={p.slot} value={p.display_number}>
                {label(p)}
              </option>
            ))}
          </select>
        </label>
      )}
      {renaming ? (
        <>
          <button
            type="button"
            className="btn preset-selector__icon"
            aria-label={t('preset.rename.apply', 'Apply name')}
            title={t('preset.rename.applyTitle', 'Rename the preset (it stays unsaved until you save)')}
            disabled={!newName}
            onClick={applyName}
          >
            <CheckIcon />
          </button>
          <button
            type="button"
            className="btn preset-selector__icon"
            aria-label={t('preset.rename.cancel', 'Cancel rename')}
            title={t('preset.rename.cancel', 'Cancel rename')}
            onClick={() => setRenaming(false)}
          >
            <CrossIcon />
          </button>
        </>
      ) : (
        <button
          type="button"
          className="btn preset-selector__icon"
          aria-label={t('preset.rename.button', 'Rename preset')}
          title={t('preset.rename.buttonTitle', 'Change the name of the preset (up to 8 characters)')}
          disabled={locked || !active}
          onClick={startRename}
        >
          <PencilIcon />
        </button>
      )}
      <button
        type="button"
        className="btn preset-selector__icon"
        aria-label={t('preset.selector.save', 'Save preset')}
        title={t('preset.selector.saveTitle', 'Store the changes in the pedal (until you save, they only live in its working memory)')}
        disabled={!canAct}
        onClick={() => void bridgeTransport.saveNow()}
      >
        <SaveIcon />
      </button>
      <button
        type="button"
        className="btn preset-selector__icon"
        aria-label={t('preset.selector.restore', 'Restore saved version')}
        title={t('preset.selector.restoreTitle', 'Throw away the changes and go back to the version stored in the pedal')}
        disabled={!canAct}
        onClick={() => void bridgeTransport.revert()}
      >
        <RestoreIcon />
      </button>
      <button
        type="button"
        className="btn preset-selector__icon"
        aria-label={t('preset.file.download', 'Download preset file')}
        title={t('preset.file.downloadTitle', 'Save this preset to a file on your computer')}
        disabled={!pedalConnected}
        onClick={() => void files.download()}
      >
        <DownloadIcon />
      </button>
      <button
        type="button"
        className="btn preset-selector__icon"
        aria-label={t('preset.file.upload', 'Upload preset file')}
        title={t('preset.file.uploadTitle', 'Replace the content of this preset with a file (applied live, not stored until you save)')}
        disabled={locked}
        onClick={() => fileInput.current?.click()}
      >
        <UploadIcon />
      </button>
      <input
        ref={fileInput}
        type="file"
        accept="application/json,.json"
        hidden
        tabIndex={-1}
        aria-hidden
        onChange={(e) => void files.upload(e)}
      />
      <label className="preset-selector__volume">
        <span>{t('preset.selector.volume', 'Volume')}</span>
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          value={volume ?? 0}
          disabled={locked || volume === null}
          aria-label={t('preset.selector.volumeLabel', 'Preset volume')}
          onChange={(e) => changeVolume(Number(e.target.value))}
        />
        <output>{volume ?? '–'}</output>
      </label>
      <div className="preset-selector__badge">
        <AutosaveBadge />
      </div>
    </div>
  );
}
