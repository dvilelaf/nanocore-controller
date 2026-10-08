import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { AutosaveBadge, BridgeErrorBanner, ConnectionStatus } from '../ConnectionStatus';
import { PresetSelector } from '../PresetSelector';
import { bridgeTransport } from '../../midi/bridgeTransport';
import type { BridgeErrorInfo } from '../../midi/bridgeTransport';
import { usePatchStore } from '../../store/patchStore';
import { useDeviceStore } from '../../store/deviceState';

const wrap = (node: React.ReactNode) => render(<I18nextProvider i18n={i18n}>{node}</I18nextProvider>);

const autosave = (over = {}) => ({
  enabled: true,
  state: 'saved' as const,
  last_saved_at: '2026-10-07T20:00:00+00:00',
  error: null,
  ...over,
});

beforeEach(() => {
  useDeviceStore.getState().reset();
  usePatchStore.setState((s) => ({ connection: { ...s.connection, transportKind: 'bridge' } }));
  useDeviceStore.setState({ link: 'connected', pedalConnected: true, autosave: autosave() });
});

afterEach(() => {
  cleanup();
  usePatchStore.setState((s) => ({ connection: { ...s.connection, transportKind: 'simulator' } }));
  vi.restoreAllMocks();
});

describe('connection indicator', () => {
  it('renders nothing for the other transports', () => {
    usePatchStore.setState((s) => ({ connection: { ...s.connection, transportKind: 'simulator' } }));
    const { container } = wrap(<ConnectionStatus />);
    expect(container).toBeEmptyDOMElement();
  });

  it('has no connection badge: the Connection button carries that state', () => {
    for (const link of ['connecting', 'connected', 'reconnecting', 'closed']) {
      useDeviceStore.setState({ link: link as never });
      const { unmount } = wrap(<ConnectionStatus />);
      expect(screen.queryByTestId('link-status')).toBeNull();
      unmount();
    }
  });

  it('shows a read-only badge only in read-only mode', () => {
    wrap(<ConnectionStatus />);
    expect(screen.queryByTestId('read-only-status')).toBeNull();
    cleanup();
    useDeviceStore.setState({ readOnly: true });
    wrap(<ConnectionStatus />);
    expect(screen.getByTestId('read-only-status')).toHaveTextContent('Read-only');
  });
});

describe('autosave indicator', () => {
  const status = () => screen.getByTestId('autosave-status');

  it('shows nothing when everything is saved', () => {
    wrap(<AutosaveBadge />);
    expect(screen.queryByTestId('autosave-status')).toBeNull();
  });

  it('shows the indicator again as soon as something is not saved, and hides it once saved', () => {
    useDeviceStore.setState({ autosave: autosave({ state: 'dirty' }) });
    wrap(<AutosaveBadge />);
    expect(status()).toHaveAttribute('role', 'status');
    act(() => useDeviceStore.setState({ autosave: autosave({ state: 'saved' }) }));
    expect(screen.queryByTestId('autosave-status')).toBeNull();
  });

  it('shows unsaved changes', () => {
    useDeviceStore.setState({ autosave: autosave({ state: 'dirty' }) });
    wrap(<AutosaveBadge />);
    expect(status()).toHaveTextContent('Unsaved changes');
    expect(status()).toHaveTextContent('●');
  });

  it('shows saving', () => {
    useDeviceStore.setState({ autosave: autosave({ state: 'saving' }) });
    wrap(<AutosaveBadge />);
    expect(status()).toHaveTextContent('Saving');
    expect(status()).not.toHaveTextContent('last saved');
  });

  it('shows the error text from the server', () => {
    useDeviceStore.setState({ autosave: autosave({ state: 'error', error: 'slot changed before saving' }) });
    wrap(<AutosaveBadge />);
    expect(status()).toHaveTextContent('Problem saving: slot changed before saving');
    expect(status()).toHaveTextContent('⚠');
  });

  it('in manual mode shows nothing when saved and "Unsaved changes" when not', () => {
    useDeviceStore.setState({ autosave: autosave({ enabled: false, state: 'saved' }) });
    const { unmount } = wrap(<AutosaveBadge />);
    expect(screen.queryByTestId('autosave-status')).toBeNull();
    unmount();
    useDeviceStore.setState({ autosave: autosave({ enabled: false, state: 'dirty' }) });
    wrap(<AutosaveBadge />);
    expect(status()).toHaveTextContent('Unsaved changes');
    expect(status()).not.toHaveTextContent('Autosave');
  });

  it('has no buttons', () => {
    wrap(<AutosaveBadge />);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});

describe('error banner', () => {
  const err = (over: Partial<BridgeErrorInfo>): BridgeErrorInfo => ({
    code: 'read_only',
    status: 403,
    message: 'server text',
    maybeApplied: false,
    lost: true,
    context: 'edit',
    ...over,
  });

  const cases: [Partial<BridgeErrorInfo>, string][] = [
    [{ code: 'unauthorized', status: 401 }, 'Reload the page using the address printed by nanocore serve'],
    [{ code: 'read_only' }, 'read-only mode'],
    [{ code: 'slot_changed', status: 409 }, 'active preset changed'],
    [{ code: 'rate_limited', status: 429, lost: false }, 'Retrying'],
    [{ code: 'rate_limited', status: 429 }, 'was not applied'],
    [{ code: 'disconnected', status: 503 }, 'pedal is not connected'],
    [{ code: 'device_timeout', status: 504, maybeApplied: true }, 'may or may not have been applied'],
    [{ code: 'device_timeout', status: 504 }, 'Your change was not applied'],
    [{ code: 'network', status: 0 }, 'lost contact'],
  ];
  for (const [over, text] of cases) {
    it(`explains ${over.code}${over.maybeApplied ? ' (maybe applied)' : ''}`, () => {
      useDeviceStore.setState({ error: err(over) });
      wrap(<BridgeErrorBanner />);
      expect(screen.getByRole('alert')).toHaveTextContent(text);
    });
  }

  it('translates the message', async () => {
    await i18n.changeLanguage('it');
    try {
      useDeviceStore.setState({ error: err({ code: 'slot_changed', status: 409 }) });
      wrap(<BridgeErrorBanner />);
      expect(screen.getByRole('alert')).toHaveTextContent('Il preset attivo è cambiato sul pedale');
    } finally {
      await i18n.changeLanguage('en');
    }
  });

  it('clears a transient error by itself', () => {
    vi.useFakeTimers();
    try {
      useDeviceStore.setState({ error: err({ code: 'rate_limited', lost: false }) });
      wrap(<BridgeErrorBanner />);
      expect(screen.getByRole('alert')).toBeInTheDocument();
      act(() => {
        vi.advanceTimersByTime(4100);
      });
      expect(screen.queryByRole('alert')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps a lost edit on screen until it is dismissed', () => {
    vi.useFakeTimers();
    try {
      useDeviceStore.setState({ error: err({}) });
      wrap(<BridgeErrorBanner />);
      act(() => {
        vi.advanceTimersByTime(60000);
      });
      fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
      expect(screen.queryByRole('alert')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('device text is rendered as text', () => {
  const hostile = '<img src=x onerror="alert(1)"> "><script>alert(2)</script>';

  it('keeps a hostile preset name inert in the header and the preset list', async () => {
    useDeviceStore.setState({ preset: { slot: 3, display_number: 4, name: hostile } });
    vi.spyOn(bridgeTransport, 'fetchPresets').mockResolvedValue([
      { slot: 3, display_number: 4, name: hostile },
      { slot: 4, display_number: 5, name: "x' onclick='alert(3)" },
    ]);
    const { container, findAllByText } = wrap(
      <>
        <ConnectionStatus />
        <PresetSelector />
      </>,
    );
    await findAllByText(hostile, { exact: false });
    expect(container.querySelector('img, script')).toBeNull();
    expect(container.querySelector('[onerror], [onclick]')).toBeNull();
    expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveTextContent(hostile);
    for (const el of container.querySelectorAll('*')) {
      for (const attr of el.getAttributeNames()) expect(el.getAttribute(attr)).not.toContain('onerror');
    }
  });

  it('keeps hostile amp and cab names inert on the chain board', async () => {
    const { ChainBoard } = await import('../ChainBoard');
    useDeviceStore.setState({
      hydrated: true,
      assets: { amp: { slot: 1, name: hostile }, ir: { slot: 2, name: '</p><b>bold</b>' } },
    });
    const { container } = wrap(<ChainBoard />);
    expect(container.querySelector('img, script, b')).toBeNull();
    expect(container.querySelector('[data-block="amp"]')).toHaveTextContent(hostile);
    expect(container.querySelector('[data-block="cab"]')).toHaveTextContent('</p><b>bold</b>');
  });
});
