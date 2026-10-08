import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import './App.css';
import { ChainBoard } from './components/ChainBoard';
import { BridgeErrorBanner, ConnectionStatus } from './components/ConnectionStatus';
import { ConnectionMenu } from './components/ConnectionMenu';
import { TunerButton } from './components/TunerButton';
import { SettingsMenu } from './components/SettingsMenu';
import { ModelsMenu } from './components/ModelsMenu';
import { LanguageSwitcher } from './components/LanguageSwitcher';
import { ThemeToggle } from './components/ThemeToggle';
import { Toast } from './components/Toast';
import { usePatchStore } from './store/patchStore';
import { detectTokenlessServer, getBridgeToken } from './midi/bridgeToken';
import { useDeviceStore } from './store/deviceState';

function App() {
  const { t } = useTranslation();
  const initTransport = usePatchStore((s) => s.initTransport);
  const isBridge = usePatchStore((s) => s.connection.transportKind === 'bridge');
  const pedalConnected = useDeviceStore((s) => s.pedalConnected);
  const released = useDeviceStore((s) => s.released);
  const setTuner = usePatchStore((s) => s.setTuner);
  const [tunerOn, setTunerOn] = useState(false);
  const [noServer, setNoServer] = useState(false);
  // The settings panel and the models panel share the bottom left corner: only one is open at a time.
  const [panel, setPanel] = useState<'settings' | 'models' | null>(null);
  const setSettingsOpen = useCallback((open: boolean) => setPanel((was) => (open ? 'settings' : was === 'settings' ? null : was)), []);
  const setModelsOpen = useCallback((open: boolean) => setPanel((was) => (open ? 'models' : was === 'models' ? null : was)), []);

  useEffect(() => {
    // The editor talks to `nanocore serve` (token in the address, or none needed). Without it there is no pedal.
    let cancelled = false;
    void (async () => {
      const bridged = getBridgeToken() !== null || (await detectTokenlessServer());
      if (cancelled) return;
      if (bridged) void initTransport('bridge');
      else setNoServer(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [initTransport]);

  const toggleTuner = () => {
    setTuner(!tunerOn);
    setTunerOn(!tunerOn);
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header__title">
          <h1>{t('app.title', 'NanoCore Editor')}</h1>
        </div>
        <div className="app-header__side">
          <ConnectionMenu />
          <ThemeToggle />
          <LanguageSwitcher />
        </div>
      </header>

      <ConnectionStatus />

      <div className="app-toasts">
        <BridgeErrorBanner />

        {isBridge && !pedalConnected && !released && (
          <Toast>
            {t('app.noPedalNotice', 'The server is not connected to a pedal right now. It keeps trying; the editor updates when the pedal is back.')}
          </Toast>
        )}

        {noServer && (
          <Toast>
            {t(
              'app.noServerNotice',
              'This page is not served by nanocore serve, so there is no pedal to talk to. Run nanocore serve and open the address it prints.',
            )}
          </Toast>
        )}
      </div>

      <main className="app-main">
        <ChainBoard />
      </main>

      <SettingsMenu open={panel === 'settings'} onOpenChange={setSettingsOpen} />
      <ModelsMenu open={panel === 'models'} onOpenChange={setModelsOpen} />
      <TunerButton on={tunerOn} onToggle={toggleTuner} />
    </div>
  );
}

export default App;
