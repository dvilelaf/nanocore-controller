import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { bridgeTransport } from '../midi/bridgeTransport';
import type { GlobalSettings, SettingsChange } from '../midi/bridgeTransport';
import { useDeviceStore } from '../store/deviceState';
import { usePatchStore } from '../store/patchStore';

const DEBOUNCE_MS = 150;
const MIDI_CHANNELS = Array.from({ length: 17 }, (_, i) => i);

type Field = keyof SettingsChange;
type Problem = 'read' | 'write' | null;

const signed = (db: number) => `${db > 0 ? '+' : ''}${db} dB`;

/**
 * The pedal's own settings (loopback, input gain, volumes, MIDI channel, Bluetooth). They belong to no preset and
 * the pedal keeps them itself, so every change is written as it is made and there is no save step.
 */
function SettingsPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const { t } = useTranslation();
  const readOnly = useDeviceStore((s) => s.readOnly);
  const pedalConnected = useDeviceStore((s) => s.pedalConnected);
  const [values, setValues] = useState<GlobalSettings | null>(null);
  const [problem, setProblem] = useState<Problem>(null);
  const timers = useRef(new Map<Field, { timer: ReturnType<typeof setTimeout>; value: number }>());
  const outstanding = useRef(new Map<Field, number>());
  const chain = useRef<Promise<void>>(Promise.resolve());
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const read = useCallback(async () => {
    try {
      const next = await bridgeTransport.fetchSettings();
      if (!alive.current) return;
      setValues(next);
      setProblem(null);
    } catch {
      if (alive.current) setProblem('read');
    }
  }, []);

  useEffect(() => {
    if (pedalConnected) void read();
  }, [pedalConnected, read]);

  // A field being dragged or still waiting to be written keeps what the user sees, not the older reply.
  const busy = (field: Field) => timers.current.has(field) || (outstanding.current.get(field) ?? 0) > 0;

  const write = (change: SettingsChange) => {
    const [field] = Object.keys(change) as Field[];
    outstanding.current.set(field, (outstanding.current.get(field) ?? 0) + 1);
    chain.current = chain.current.then(async () => {
      try {
        const next = await bridgeTransport.writeSettings(change);
        outstanding.current.set(field, (outstanding.current.get(field) ?? 1) - 1);
        if (!alive.current) return;
        setValues((old) => {
          if (!old) return next;
          const merged: GlobalSettings = { ...next };
          for (const key of Object.keys(change) as Field[]) {
            if (busy(key)) (merged as unknown as Record<string, unknown>)[key] = old[key];
          }
          return merged;
        });
        setProblem(null);
      } catch {
        outstanding.current.set(field, (outstanding.current.get(field) ?? 1) - 1);
        if (!alive.current) return;
        setProblem('write');
        await read();
        if (alive.current) setProblem('write');
      }
    });
  };

  const setNow = <K extends Field>(field: K, value: GlobalSettings[K]) => {
    setValues((old) => (old ? { ...old, [field]: value } : old));
    write({ [field]: value } as SettingsChange);
  };

  const setDebounced = (field: Field, value: number) => {
    setValues((old) => (old ? { ...old, [field]: value } : old));
    const waiting = timers.current.get(field);
    if (waiting) clearTimeout(waiting.timer);
    const timer = setTimeout(() => {
      timers.current.delete(field);
      write({ [field]: value } as SettingsChange);
    }, DEBOUNCE_MS);
    timers.current.set(field, { timer, value });
  };

  const latestWrite = useRef(write);
  latestWrite.current = write;
  const flush = useCallback(() => {
    for (const [field, { timer, value }] of timers.current) {
      clearTimeout(timer);
      timers.current.delete(field);
      latestWrite.current({ [field]: value } as SettingsChange);
    }
  }, []);

  useEffect(
    () => () => {
      // Closing the panel must not lose a slider move that is still waiting for its debounce.
      flush();
    },
    [flush],
  );

  const locked = readOnly || !pedalConnected || values === null;
  const ids = { loopback: useId(), gain: useId(), usb: useId(), bt: useId(), channel: useId(), wireless: useId(), warn: useId() };

  const slider = (field: 'input_gain_db' | 'usb_volume' | 'bt_volume', htmlId: string, label: string, min: number, max: number, show: (n: number) => string) => (
    <div className="settings-row">
      <label htmlFor={htmlId}>{label}</label>
      <output htmlFor={htmlId}>{values ? show(values[field]) : '–'}</output>
      <input
        id={htmlId}
        type="range"
        min={min}
        max={max}
        step={1}
        value={values ? values[field] : min}
        disabled={locked}
        onChange={(e) => setDebounced(field, Number(e.target.value))}
      />
    </div>
  );

  return (
    <div id={id} className="settings-panel" role="dialog" aria-label={t('settings.title', 'Pedal settings')}>
      <div className="settings-panel__head">
        <h2 className="settings-panel__title">{t('settings.title', 'Pedal settings')}</h2>
        <button
          type="button"
          className="btn btn--ghost settings-panel__close"
          aria-label={t('settings.close', 'Close')}
          onClick={() => {
            flush();
            onClose();
          }}
        >
          <span aria-hidden>×</span>
        </button>
      </div>

      {readOnly && <p className="settings-panel__note">{t('settings.readOnly', 'The server is read-only: the settings cannot be changed.')}</p>}
      {!readOnly && !pedalConnected && (
        <p className="settings-panel__note">{t('settings.disconnected', 'The pedal is not connected.')}</p>
      )}

      <div className="settings-row settings-row--switch">
        <label htmlFor={ids.loopback}>{t('settings.loopback', 'Loopback')}</label>
        <input
          id={ids.loopback}
          type="checkbox"
          role="switch"
          className="settings-switch"
          checked={values?.loopback_enabled ?? false}
          disabled={locked}
          onChange={(e) => setNow('loopback_enabled', e.target.checked)}
        />
      </div>

      {slider('input_gain_db', ids.gain, t('settings.inputGain', 'Input gain'), -20, 20, signed)}
      {slider('usb_volume', ids.usb, t('settings.usbVolume', 'USB volume'), 0, 100, String)}
      {slider('bt_volume', ids.bt, t('settings.btVolume', 'Bluetooth volume'), 0, 100, String)}

      <div className="settings-row settings-row--select">
        <label htmlFor={ids.channel}>{t('settings.midiChannel', 'MIDI channel')}</label>
        <select
          id={ids.channel}
          value={values?.midi_channel ?? 0}
          disabled={locked}
          onChange={(e) => setNow('midi_channel', Number(e.target.value))}
        >
          {MIDI_CHANNELS.map((n) => (
            <option key={n} value={n}>
              {n === 0 ? t('settings.omni', 'Omni') : n}
            </option>
          ))}
        </select>
      </div>

      <div className="settings-row settings-row--switch">
        <label htmlFor={ids.wireless}>{t('settings.wireless', 'Bluetooth of the pedal')}</label>
        <input
          id={ids.wireless}
          type="checkbox"
          role="switch"
          className="settings-switch"
          aria-describedby={ids.warn}
          checked={values?.wireless_enabled ?? false}
          disabled={locked}
          onChange={(e) => setNow('wireless_enabled', e.target.checked)}
        />
      </div>
      <p id={ids.warn} className="settings-panel__warning">
        {t('settings.wirelessWarning', 'Turning it off drops the Bluetooth connection, also the one this editor may be using.')}
      </p>

      {problem && (
        <p className="settings-panel__error" role="alert">
          {problem === 'read'
            ? t('settings.readError', 'The settings could not be read from the pedal.')
            : t('settings.writeError', 'The setting could not be changed. Showing what the pedal has.')}
        </p>
      )}
    </div>
  );
}

function GearIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

/** Floating gear button next to the tuner button; it opens the settings panel at the bottom left of the screen. */
export function SettingsMenu({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
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
  const label = t('settings.toggle', 'Settings');
  return (
    <div className="settings-menu" ref={root}>
      <button
        type="button"
        ref={button}
        className="btn fab settings-fab"
        aria-label={label}
        title={t('settings.toggleTitle', 'Settings of the pedal itself (loopback, input gain, volumes, MIDI channel, Bluetooth)')}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => onOpenChange(!open)}
      >
        <GearIcon />
      </button>
      {open && <SettingsPanel id={panelId} onClose={() => onOpenChange(false)} />}
    </div>
  );
}
