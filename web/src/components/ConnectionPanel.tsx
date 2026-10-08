import { useTranslation } from 'react-i18next';
import { usePatchStore } from '../store/patchStore';
import { useDeviceStore } from '../store/deviceState';
import { bridgeTransport } from '../midi/bridgeTransport';

function SwitchRow({ label, on, onToggle }: { label: string; on: boolean; onToggle: () => void }) {
  return (
    <div className="connection-panel__row">
      <span className="connection-panel__field-label">{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={label}
        className={`switch ${on ? 'switch--on' : ''}`}
        onClick={onToggle}
      >
        <span className="switch__knob" aria-hidden />
      </button>
    </div>
  );
}

/** Two switches: whether the server holds the pedal's USB port, and whether the guitar plays through the PC speakers. */
export function ConnectionPanel() {
  const { t } = useTranslation();
  const isBridge = usePatchStore((s) => s.connection.transportKind === 'bridge');
  const released = useDeviceStore((s) => s.released);
  const audio = useDeviceStore((s) => s.audio);

  return (
    <section className="connection-panel" aria-label={t('connection.menu', 'Connection')}>
      {isBridge && (
        <>
          <SwitchRow
            label={t('connection.switchPedal', 'Pedal')}
            on={!released}
            onToggle={() => void (released ? bridgeTransport.resume() : bridgeTransport.release())}
          />
          {audio.available && (
            <SwitchRow
              label={t('connection.switchSpeakers', 'Speakers')}
              on={audio.on}
              onToggle={() => void bridgeTransport.setAudio(!audio.on)}
            />
          )}
        </>
      )}
    </section>
  );
}
