import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useDeviceStore } from '../store/deviceState';
import { usePatchStore } from '../store/patchStore';
import { ConnectionPanel } from './ConnectionPanel';

type State = 'connected' | 'connecting' | 'disconnected';

/** Whether a real device is reachable: the pedal through the server, or a local MIDI port. */
function useDeviceState(): State {
  const isBridge = usePatchStore((s) => s.connection.transportKind === 'bridge');
  const kind = usePatchStore((s) => s.connection.transportKind);
  const ready = usePatchStore((s) => s.connection.ready);
  const initializing = usePatchStore((s) => s.connection.initializing);
  const link = useDeviceStore((s) => s.link);
  const pedalConnected = useDeviceStore((s) => s.pedalConnected);

  if (isBridge) {
    if (link === 'connecting' || link === 'reconnecting') return 'connecting';
    return link === 'connected' && pedalConnected ? 'connected' : 'disconnected';
  }
  if (initializing) return 'connecting';
  return ready && kind !== 'simulator' ? 'connected' : 'disconnected';
}

/** Header button that opens the MIDI connection panel as a dropdown instead of a permanent block. */
export function ConnectionMenu() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const state = useDeviceState();
  const stateText = {
    connected: t('connection.stateConnected', 'connected'),
    connecting: t('connection.stateConnecting', 'connecting'),
    disconnected: t('connection.stateDisconnected', 'disconnected'),
  }[state];
  const label = t('connection.menuState', 'Connection: {{state}}', { state: stateText });

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    const onPointer = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onPointer);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onPointer);
    };
  }, [open]);

  return (
    <div className="connection-menu" ref={root}>
      <button
        type="button"
        className={`btn connection-menu__button connection-menu__button--${state}`}
        aria-label={label}
        title={label}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden>⚙ </span>
        {t('connection.menu', 'Connection')}
      </button>
      {open && (
        <div id={panelId} className="connection-menu__panel">
          <ConnectionPanel />
        </div>
      )}
    </div>
  );
}
