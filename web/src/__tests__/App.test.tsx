import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../i18n';
import App from '../App';
import { usePatchStore } from '../store/patchStore';
import { useDeviceStore } from '../store/deviceState';
import { captureTokenFromUrl, resetBridgeTokenForTests } from '../midi/bridgeToken';
import { FakeWebSocket, makeFetch, makeState } from '../test/bridgeFakes';

const renderApp = () =>
  render(
    <I18nextProvider i18n={i18n}>
      <App />
    </I18nextProvider>,
  );

beforeEach(() => {
  FakeWebSocket.reset();
  useDeviceStore.getState().reset();
});

afterEach(async () => {
  cleanup();
  await usePatchStore.getState().initTransport('test');
  resetBridgeTokenForTests();
  vi.unstubAllGlobals();
});

describe('App without the server', () => {
  it('connects to nothing and says so when the page is not served by the server', async () => {
    renderApp();
    const notice = await screen.findByText(/not served by nanocore serve/);
    expect(usePatchStore.getState().connection.transportKind).toBe('none');
    expect(usePatchStore.getState().connection.ready).toBe(false);
    expect(screen.queryByTestId('link-status')).toBeNull();
    expect(notice.closest('.app-toasts')).not.toBeNull();
    expect(screen.queryByRole('button', { name: 'Send patch to device' })).toBeNull();
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument();
  });
});

describe('App served by nanocore serve', () => {
  async function renderBridged() {
    const doc = makeState();
    const f = makeFetch((call) => {
      if (call.path === '/api/presets') {
        return {
          body: {
            presets: [
              { slot: 8, display_number: 9, name: 'FunkCln' },
              { slot: 9, display_number: 10, name: 'Tremolo' },
            ],
          },
        };
      }
      if (call.path === '/api/assets') return { body: { amp: null, ir: null } };
      return { body: doc };
    });
    vi.stubGlobal('fetch', f.fn);
    vi.stubGlobal('WebSocket', FakeWebSocket);
    captureTokenFromUrl({ hash: '#token=t', pathname: '/', search: '' }, { replaceState: vi.fn(), state: null });
    renderApp();
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Preset' })).toBeInTheDocument());
    return f;
  }

  it('connects by itself and shows the connection, the preset and the autosave state', async () => {
    await renderBridged();
    expect(usePatchStore.getState().connection.transportKind).toBe('bridge');
    expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveDisplayValue('9 · FunkCln');
    expect(screen.queryByTestId('autosave-status')).toBeNull();
    expect(screen.queryByText(/No NanoCore connected yet/)).toBeNull();
  });

  it('says nothing when the pedal was released on purpose, and shows the switch off', async () => {
    await renderBridged();
    FakeWebSocket.last().receive({ type: 'connection', connected: false, reason: null, released: true });
    await waitFor(() => expect(useDeviceStore.getState().released).toBe(true));
    expect(screen.queryByText(/not connected to a pedal/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Connection/ }));
    expect(screen.getByRole('switch', { name: 'Pedal' })).toHaveAttribute('aria-checked', 'false');
  });

  it('still warns when the pedal is missing and nobody released it', async () => {
    await renderBridged();
    FakeWebSocket.last().receive({ type: 'connection', connected: false, reason: 'gone', released: false });
    await waitFor(() => expect(screen.getByText(/not connected to a pedal/)).toBeInTheDocument());
  });

  it('keeps the MIDI connection panel out of the way until the menu button is used', async () => {
    await renderBridged();
    expect(screen.queryByRole('region', { name: 'Connection' })).toBeNull();
    const button = screen.getByRole('button', { name: /Connection/ });
    expect(button).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(button);
    expect(screen.getByRole('region', { name: 'Connection' })).toBeInTheDocument();
    expect(button).toHaveAttribute('aria-expanded', 'true');

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('region', { name: 'Connection' })).toBeNull();
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });

  it('closes the connection menu when clicking elsewhere', async () => {
    await renderBridged();
    fireEvent.click(screen.getByRole('button', { name: /Connection/ }));
    expect(screen.getByRole('region', { name: 'Connection' })).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('region', { name: 'Connection' })).toBeNull();
  });

  it('is a single view: no tab bar, no sidebar, just the chain', async () => {
    await renderBridged();
    expect(screen.queryByRole('navigation')).toBeNull();
    expect(document.querySelector('.app-tabs, .app-sidebar')).toBeNull();
    for (const name of ['Effect Chain', 'Global Settings', 'MIDI Activity', 'About', 'Presets']) {
      expect(screen.queryByRole('button', { name })).toBeNull();
    }
    expect(document.querySelector('main.app-main [data-block]')).not.toBeNull();
  });

  it('does not show the long subtitle under the title', async () => {
    await renderBridged();
    expect(screen.queryByText(/Unofficial online editor/)).toBeNull();
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument();
  });

  it('has a preset selector in the header that lists the pedal presets and recalls the chosen one', async () => {
    const f = await renderBridged();
    const select = await screen.findByRole('combobox', { name: 'Preset' });
    await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(2));
    expect(within(select).getByRole('option', { name: '10 · Tremolo' })).toBeInTheDocument();

    fireEvent.change(select, { target: { value: '10' } });
    await waitFor(() =>
      expect(f.calls.some((c) => c.path === '/api/preset' && (c.body as { display_number: number }).display_number === 10)).toBe(true),
    );
  });

  it('has only the dropdown for presets, with no step buttons beside it', async () => {
    await renderBridged();
    await screen.findByRole('combobox', { name: 'Preset' });
    expect(screen.queryByRole('button', { name: 'Previous preset' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Next preset' })).toBeNull();
  });

  it('fills the Connection button green when the pedal is connected', async () => {
    await renderBridged();
    act(() => useDeviceStore.setState({ link: 'connected', pedalConnected: true }));
    const button = screen.getByRole('button', { name: /Connection/ });
    expect(button).toHaveClass('connection-menu__button--connected');
    expect(button).toHaveAccessibleName('Connection: connected');
  });

  it('fills the Connection button red when the pedal or the server is gone, amber while connecting', async () => {
    await renderBridged();
    const button = screen.getByRole('button', { name: /Connection/ });
    act(() => useDeviceStore.setState({ link: 'connected', pedalConnected: true }));

    act(() => useDeviceStore.setState({ pedalConnected: false }));
    expect(button).toHaveClass('connection-menu__button--disconnected');
    expect(button).toHaveAccessibleName('Connection: disconnected');

    act(() => useDeviceStore.setState({ pedalConnected: true, link: 'reconnecting' }));
    expect(button).toHaveClass('connection-menu__button--connecting');
    expect(button).toHaveAccessibleName('Connection: connecting');

    act(() => useDeviceStore.setState({ link: 'closed' }));
    expect(button).toHaveClass('connection-menu__button--disconnected');
  });

  it('shows the Connection button as disconnected for a transport that is not the server', async () => {
    renderApp();
    await waitFor(() => expect(usePatchStore.getState().connection.ready).toBe(true));
    expect(screen.getByRole('button', { name: /Connection/ })).toHaveClass('connection-menu__button--disconnected');
  });

  describe('saving by hand', () => {
    const dirty = () =>
      act(() =>
        useDeviceStore.setState({
          autosave: { enabled: false, state: 'dirty', last_saved_at: null, error: null },
        }),
      );
    const saveButton = () => screen.getByRole('button', { name: 'Save preset' });
    const restoreButton = () => screen.getByRole('button', { name: 'Restore saved version' });
    const posts = (f: { calls: { method: string; path: string; body: unknown }[] }, path: string) =>
      f.calls.filter((c) => c.method === 'POST' && c.path === path);

    it('puts a save and a restore icon button to the right of the preset dropdown', async () => {
      await renderBridged();
      const select = await screen.findByRole('combobox', { name: 'Preset' });
      const row = select.closest('.preset-selector')!;
      expect(row.contains(saveButton())).toBe(true);
      expect(row.contains(restoreButton())).toBe(true);
      const order = [...row.querySelectorAll('select, button')].map((e) => e.tagName + (e.getAttribute('aria-label') ?? ''));
      expect(order.slice(0, 4)).toEqual(['SELECT', 'BUTTONRename preset', 'BUTTONSave preset', 'BUTTONRestore saved version']);
      expect(saveButton().querySelector('svg')).not.toBeNull();
    });

    it('shows "Unsaved changes" to the right of the save and restore buttons, in the same row', async () => {
      await renderBridged();
      const select = await screen.findByRole('combobox', { name: 'Preset' });
      const row = select.closest('.preset-selector')!;
      expect(row.querySelector('[data-testid="autosave-status"]')).toBeNull();
      dirty();
      const badge = row.querySelector('[data-testid="autosave-status"]');
      expect(badge).not.toBeNull();
      expect(badge).toHaveTextContent('Unsaved changes');
      const order = [...row.querySelectorAll('select, button, [data-testid="autosave-status"]')].map(
        (e) => e.getAttribute('data-testid') ?? e.getAttribute('aria-label') ?? e.tagName,
      );
      expect(order).toEqual([
        'SELECT',
        'Rename preset',
        'Save preset',
        'Restore saved version',
        'Download preset file',
        'Upload preset file',
        'autosave-status',
      ]);
      expect(document.querySelector('.conn-status [data-testid="autosave-status"]')).toBeNull();
    });

    it('keeps both buttons disabled while nothing is unsaved', async () => {
      await renderBridged();
      await screen.findByRole('combobox', { name: 'Preset' });
      expect(saveButton()).toBeDisabled();
      expect(restoreButton()).toBeDisabled();
    });

    it('enables them once there are unsaved changes', async () => {
      await renderBridged();
      await screen.findByRole('combobox', { name: 'Preset' });
      dirty();
      expect(saveButton()).toBeEnabled();
      expect(restoreButton()).toBeEnabled();
    });

    it('saves with the save button, and does not save by itself', async () => {
      const f = await renderBridged();
      await screen.findByRole('combobox', { name: 'Preset' });
      dirty();
      expect(posts(f, '/api/save-now')).toHaveLength(0);
      fireEvent.click(saveButton());
      await waitFor(() => expect(posts(f, '/api/save-now')).toHaveLength(1));
    });

    it('restores the stored version with the restore button', async () => {
      const f = await renderBridged();
      await screen.findByRole('combobox', { name: 'Preset' });
      dirty();
      fireEvent.click(restoreButton());
      await waitFor(() => expect(posts(f, '/api/revert')).toHaveLength(1));
    });

    it('disables both buttons in read-only mode', async () => {
      await renderBridged();
      await screen.findByRole('combobox', { name: 'Preset' });
      dirty();
      act(() => useDeviceStore.setState({ readOnly: true }));
      expect(saveButton()).toBeDisabled();
      expect(restoreButton()).toBeDisabled();
    });

    it('asks before switching preset with unsaved changes, and only then discards them', async () => {
      const f = await renderBridged();
      const select = await screen.findByRole('combobox', { name: 'Preset' });
      await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(2));
      dirty();
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
      fireEvent.change(select, { target: { value: '10' } });
      expect(confirm).toHaveBeenCalledTimes(1);
      await new Promise((r) => setTimeout(r, 30));
      expect(posts(f, '/api/preset')).toHaveLength(0);

      confirm.mockReturnValue(true);
      fireEvent.change(select, { target: { value: '10' } });
      await waitFor(() => expect(posts(f, '/api/preset')).toHaveLength(1));
      expect(posts(f, '/api/preset')[0].body).toEqual({ display_number: 10, discard: true });
      confirm.mockRestore();
    });

    it('switches preset without asking when nothing is unsaved', async () => {
      const f = await renderBridged();
      const select = await screen.findByRole('combobox', { name: 'Preset' });
      await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(2));
      const confirm = vi.spyOn(window, 'confirm');
      fireEvent.change(select, { target: { value: '10' } });
      await waitFor(() => expect(posts(f, '/api/preset')).toHaveLength(1));
      expect(confirm).not.toHaveBeenCalled();
      expect(posts(f, '/api/preset')[0].body).toEqual({ display_number: 10, discard: false });
      confirm.mockRestore();
    });
  });

  describe('preset volume', () => {
    const volumeSlider = () => screen.getByRole('slider', { name: 'Preset volume' });
    const volumeOps = (f: { calls: { method: string; path: string; body: unknown }[] }) =>
      f.calls
        .filter((c) => c.method === 'POST' && c.path === '/api/edit')
        .flatMap((c) => (c.body as { ops: { op: string; value: number }[] }).ops)
        .filter((o) => o.op === 'volume')
        .map((o) => o.value);

    it('shows the volume of the preset next to the other preset controls', async () => {
      await renderBridged();
      const select = await screen.findByRole('combobox', { name: 'Preset' });
      const live = useDeviceStore.getState().doc!.live!.volume;
      expect(volumeSlider()).toHaveValue(String(live));
      expect(select.closest('.preset-selector')!.contains(volumeSlider())).toBe(true);
      expect(volumeSlider()).toHaveAttribute('min', '0');
      expect(volumeSlider()).toHaveAttribute('max', '100');
    });

    it('sends the volume as an edit, one request for a quick drag, and shows it at once', async () => {
      const f = await renderBridged();
      await screen.findByRole('combobox', { name: 'Preset' });
      for (const v of ['40', '45', '50']) fireEvent.change(volumeSlider(), { target: { value: v } });
      expect(volumeSlider()).toHaveValue('50');
      await waitFor(() => expect(volumeOps(f)).toEqual([50]));
    });

    it('is locked without a pedal and in read-only mode', async () => {
      await renderBridged();
      await screen.findByRole('combobox', { name: 'Preset' });
      act(() => useDeviceStore.setState({ pedalConnected: false }));
      expect(volumeSlider()).toBeDisabled();
      act(() => useDeviceStore.setState({ pedalConnected: true, readOnly: true }));
      expect(volumeSlider()).toBeDisabled();
    });
  });

  describe('floating tuner button', () => {
    const tunerButton = () => screen.getByRole('button', { name: 'Tuner' });
    const ccPosts = (f: { calls: { method: string; path: string; body: unknown }[] }) =>
      f.calls
        .filter((c) => c.method === 'POST' && c.path === '/api/edit')
        .flatMap((c) => (c.body as { ops: { op: string; cc: number; value: number }[] }).ops)
        .filter((o) => o.op === 'cc' && o.cc === 80)
        .map((o) => o.value);

    it('is a floating button', async () => {
      await renderBridged();
      const button = tunerButton();
      expect(button).toHaveClass('tuner-fab');
      expect(button).toHaveAttribute('aria-pressed', 'false');
    });

    it('is an icon without visible text, and still has the name Tuner for assistive technology', async () => {
      await renderBridged();
      const button = tunerButton();
      expect(button.textContent).toBe('');
      expect(button.querySelector('svg')).not.toBeNull();
      expect(button).toHaveAttribute('aria-label', 'Tuner');
      expect(button).toHaveAttribute('title');
    });

    it('turns the tuner on and off with CC 80', async () => {
      const f = await renderBridged();
      act(() => useDeviceStore.setState({ link: 'connected', pedalConnected: true }));
      fireEvent.click(tunerButton());
      expect(tunerButton()).toHaveAttribute('aria-pressed', 'true');
      await waitFor(() => expect(ccPosts(f)).toEqual([127]));
      fireEvent.click(tunerButton());
      expect(tunerButton()).toHaveAttribute('aria-pressed', 'false');
      await waitFor(() => expect(ccPosts(f)).toEqual([127, 0]));
    });

    it('is disabled without a pedal and in read-only mode', async () => {
      await renderBridged();
      act(() => useDeviceStore.setState({ pedalConnected: false }));
      expect(tunerButton()).toBeDisabled();
      act(() => useDeviceStore.setState({ pedalConnected: true, readOnly: true }));
      expect(tunerButton()).toBeDisabled();
    });
  });

  it('has no send-patch or local library flow, and only the preset save button', async () => {
    await renderBridged();
    expect(screen.queryByRole('button', { name: /send patch/i })).toBeNull();
    expect(screen.getAllByRole('button', { name: /^save/i }).map((b) => b.getAttribute('aria-label'))).toEqual(['Save preset']);
    expect(screen.queryByRole('button', { name: /send patch/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /save as new preset/i })).toBeNull();
  });
});
