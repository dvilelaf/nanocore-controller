import { useTranslation } from 'react-i18next';
import { useDeviceStore } from '../store/deviceState';
import { usePatchStore } from '../store/patchStore';

interface Props {
  on: boolean;
  onToggle: () => void;
}

/** Floating tuner switch, drawn as a tuning fork. Whether the tuner is on is kept by the caller, so it can be turned off. */
export function TunerButton({ on, onToggle }: Props) {
  const { t } = useTranslation();
  const isBridge = usePatchStore((s) => s.connection.transportKind === 'bridge');
  const ready = usePatchStore((s) => s.connection.ready);
  const pedalConnected = useDeviceStore((s) => s.pedalConnected);
  const readOnly = useDeviceStore((s) => s.readOnly);

  const usable = isBridge ? pedalConnected && !readOnly : ready;
  const label = t('tuner.toggle', 'Tuner');

  return (
    <button
      type="button"
      className={`btn fab tuner-fab ${on ? 'tuner-fab--on' : ''}`}
      aria-pressed={on}
      aria-label={label}
      title={t(
        'tuner.note',
        'Sends CC80 (0 = off, 127 = on) to remotely open/close the on-device tuner. The reference pitch (default 440Hz) is only adjustable on the device itself — no CC is documented for it.',
      )}
      disabled={!usable}
      onClick={onToggle}
    >
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M8 3v8a4 4 0 0 0 8 0V3" />
        <path d="M12 15v6" />
      </svg>
    </button>
  );
}
