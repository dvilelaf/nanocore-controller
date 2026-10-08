import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { ConnectionPanel } from '../ConnectionPanel';
import { bridgeTransport } from '../../midi/bridgeTransport';
import { usePatchStore } from '../../store/patchStore';
import { useDeviceStore } from '../../store/deviceState';

const wrap = (node: React.ReactNode) => render(<I18nextProvider i18n={i18n}>{node}</I18nextProvider>);

beforeEach(() => {
  useDeviceStore.getState().reset();
  usePatchStore.setState((s) => ({ connection: { ...s.connection, transportKind: 'bridge', ready: true } }));
  useDeviceStore.setState({ link: 'connected', pedalConnected: true });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Pedal switch', () => {
  it('is on while the server holds the pedal and releases it when clicked', () => {
    const release = vi.spyOn(bridgeTransport, 'release').mockResolvedValue();
    wrap(<ConnectionPanel />);
    const toggle = screen.getByRole('switch', { name: 'Pedal' });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(toggle);
    expect(release).toHaveBeenCalledTimes(1);
  });

  it('is off once released and takes the pedal back when clicked', () => {
    useDeviceStore.setState({ released: true, pedalConnected: false });
    const resume = vi.spyOn(bridgeTransport, 'resume').mockResolvedValue();
    wrap(<ConnectionPanel />);
    const toggle = screen.getByRole('switch', { name: 'Pedal' });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(toggle);
    expect(resume).toHaveBeenCalledTimes(1);
  });

  it('stays on while the server is still looking for the pedal', () => {
    useDeviceStore.setState({ pedalConnected: false, released: false });
    wrap(<ConnectionPanel />);
    expect(screen.getByRole('switch', { name: 'Pedal' })).toHaveAttribute('aria-checked', 'true');
  });
});

describe('Speakers switch', () => {
  it('is not shown when the server does not control the audio', () => {
    useDeviceStore.setState({ audio: { available: false, on: false } });
    wrap(<ConnectionPanel />);
    expect(screen.queryByRole('switch', { name: 'Speakers' })).toBeNull();
  });

  it('is on when the guitar plays through the speakers and turns them off when clicked', () => {
    useDeviceStore.setState({ audio: { available: true, on: true } });
    const setAudio = vi.spyOn(bridgeTransport, 'setAudio').mockResolvedValue();
    wrap(<ConnectionPanel />);
    const toggle = screen.getByRole('switch', { name: 'Speakers' });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(toggle);
    expect(setAudio).toHaveBeenCalledWith(false);
  });

  it('turns the speakers on when clicked while off', () => {
    useDeviceStore.setState({ audio: { available: true, on: false } });
    const setAudio = vi.spyOn(bridgeTransport, 'setAudio').mockResolvedValue();
    wrap(<ConnectionPanel />);
    const toggle = screen.getByRole('switch', { name: 'Speakers' });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(toggle);
    expect(setAudio).toHaveBeenCalledWith(true);
  });

  it('works whether or not the pedal is released', () => {
    useDeviceStore.setState({ released: true, pedalConnected: false, audio: { available: true, on: true } });
    wrap(<ConnectionPanel />);
    expect(screen.getByRole('switch', { name: 'Speakers' })).not.toBeDisabled();
  });
});
