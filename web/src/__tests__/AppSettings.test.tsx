import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../i18n';
import App from '../App';
import { usePatchStore } from '../store/patchStore';
import { useDeviceStore } from '../store/deviceState';
import { captureTokenFromUrl, resetBridgeTokenForTests } from '../midi/bridgeToken';
import { FakeWebSocket, makeFetch, makeState } from '../test/bridgeFakes';
import type { Call } from '../test/bridgeFakes';
import type { GlobalSettings } from '../midi/bridgeTransport';

const settings: GlobalSettings = {
  version: 1,
  wireless_enabled: true,
  loopback_enabled: false,
  input_gain_db: 1,
  usb_volume: 80,
  bt_volume: 60,
  midi_channel: 0,
  volume_floor: null,
  volume_ceiling: null,
};

let current: GlobalSettings;
let failWrites: boolean;

async function renderBridged() {
  current = { ...settings };
  failWrites = false;
  const doc = makeState();
  const f = makeFetch((call: Call) => {
    if (call.path === '/api/presets') return { body: { presets: [{ slot: 8, display_number: 9, name: 'FunkCln' }] } };
    if (call.path === '/api/assets') return { body: { amp: null, ir: null } };
    if (call.path === '/api/settings') {
      if (call.method === 'POST') {
        if (failWrites) return { status: 502, body: { error: { code: 'verification_failed', message: 'the pedal did not take: usb_volume' } } };
        current = { ...current, ...(call.body as object) };
      }
      return { body: current };
    }
    return { body: doc };
  });
  vi.stubGlobal('fetch', f.fn);
  vi.stubGlobal('WebSocket', FakeWebSocket);
  captureTokenFromUrl({ hash: '#token=t', pathname: '/', search: '' }, { replaceState: vi.fn(), state: null });
  render(
    <I18nextProvider i18n={i18n}>
      <App />
    </I18nextProvider>,
  );
  await screen.findByRole('combobox', { name: 'Preset' });
  act(() => useDeviceStore.setState({ link: 'connected', pedalConnected: true }));
  return f;
}

const gear = () => screen.getByRole('button', { name: 'Settings' });
const panel = () => screen.getByRole('dialog', { name: 'Pedal settings' });
const settingsCalls = (f: { calls: Call[] }, method: string) => f.calls.filter((c) => c.path === '/api/settings' && c.method === method);

async function openPanel() {
  const f = await renderBridged();
  fireEvent.click(gear());
  await waitFor(() => expect(within(panel()).getByLabelText('Loopback')).toBeEnabled());
  return f;
}

beforeEach(() => {
  FakeWebSocket.reset();
  useDeviceStore.getState().reset();
});

afterEach(async () => {
  cleanup();
  await usePatchStore.getState().initTransport('simulator');
  resetBridgeTokenForTests();
  vi.unstubAllGlobals();
});

describe('settings button', () => {
  it('is a floating icon button next to the tuner button, with the same size and style', async () => {
    await renderBridged();
    const tuner = screen.getByRole('button', { name: 'Tuner' });
    expect(gear().textContent).toBe('');
    expect(gear().querySelector('svg')).not.toBeNull();
    expect(gear()).toHaveClass('fab', 'settings-fab');
    expect(tuner).toHaveClass('fab', 'tuner-fab');
    expect(gear()).toHaveAttribute('aria-expanded', 'false');
  });

  it('is not offered outside the server mode, where there are no pedal settings', async () => {
    render(
      <I18nextProvider i18n={i18n}>
        <App />
      </I18nextProvider>,
    );
    await waitFor(() => expect(usePatchStore.getState().connection.ready).toBe(true));
    expect(screen.queryByRole('button', { name: 'Settings' })).toBeNull();
  });

  it('keeps the panel closed until it is used, and does not read the settings before', async () => {
    const f = await renderBridged();
    expect(screen.queryByRole('dialog', { name: 'Pedal settings' })).toBeNull();
    expect(settingsCalls(f, 'GET')).toHaveLength(0);
  });
});

describe('settings panel', () => {
  it('loads the settings when it opens and shows every control with its value', async () => {
    const f = await openPanel();
    expect(settingsCalls(f, 'GET')).toHaveLength(1);
    expect(gear()).toHaveAttribute('aria-expanded', 'true');
    const p = within(panel());
    expect(p.getByLabelText('Loopback')).not.toBeChecked();
    expect(p.getByLabelText('Input gain')).toHaveValue('1');
    expect(p.getByLabelText('Input gain')).toHaveAttribute('min', '-20');
    expect(p.getByLabelText('Input gain')).toHaveAttribute('max', '20');
    expect(p.getByText('+1 dB')).toBeInTheDocument();
    expect(p.getByLabelText('USB volume')).toHaveValue('80');
    expect(p.getByLabelText('Bluetooth volume')).toHaveValue('60');
    expect(p.getByLabelText('MIDI channel')).toHaveDisplayValue('Omni');
    expect(within(p.getByLabelText('MIDI channel')).getAllByRole('option')).toHaveLength(17);
    expect(p.getByLabelText('Bluetooth of the pedal')).toBeChecked();
    expect(p.getByText(/drops the Bluetooth connection/)).toBeInTheDocument();
  });

  it('writes a switch as soon as it changes, sending only that field, with no save button', async () => {
    const f = await openPanel();
    fireEvent.click(within(panel()).getByLabelText('Loopback'));
    await waitFor(() => expect(settingsCalls(f, 'POST')).toHaveLength(1));
    expect(settingsCalls(f, 'POST')[0].body).toEqual({ loopback_enabled: true });
    expect(within(panel()).queryByRole('button', { name: /save/i })).toBeNull();
    await waitFor(() => expect(within(panel()).getByLabelText('Loopback')).toBeChecked());
  });

  it('writes the MIDI channel and the pedal Bluetooth switch', async () => {
    const f = await openPanel();
    fireEvent.change(within(panel()).getByLabelText('MIDI channel'), { target: { value: '3' } });
    await waitFor(() => expect(settingsCalls(f, 'POST')).toHaveLength(1));
    expect(settingsCalls(f, 'POST')[0].body).toEqual({ midi_channel: 3 });
    fireEvent.click(within(panel()).getByLabelText('Bluetooth of the pedal'));
    await waitFor(() => expect(settingsCalls(f, 'POST')).toHaveLength(2));
    expect(settingsCalls(f, 'POST')[1].body).toEqual({ wireless_enabled: false });
  });

  it('debounces a slider: many moves make one write with only that field and the last value', async () => {
    const f = await openPanel();
    const slider = within(panel()).getByLabelText('Input gain');
    fireEvent.change(slider, { target: { value: '3' } });
    fireEvent.change(slider, { target: { value: '4' } });
    fireEvent.change(slider, { target: { value: '-5' } });
    expect(within(panel()).getByText('-5 dB')).toBeInTheDocument(); // shown at once
    expect(settingsCalls(f, 'POST')).toHaveLength(0);
    await waitFor(() => expect(settingsCalls(f, 'POST')).toHaveLength(1));
    expect(settingsCalls(f, 'POST')[0].body).toEqual({ input_gain_db: -5 });
    await new Promise((r) => setTimeout(r, 250));
    expect(settingsCalls(f, 'POST')).toHaveLength(1);
  });

  it('writes the volumes as separate fields', async () => {
    const f = await openPanel();
    fireEvent.change(within(panel()).getByLabelText('USB volume'), { target: { value: '10' } });
    fireEvent.change(within(panel()).getByLabelText('Bluetooth volume'), { target: { value: '20' } });
    await waitFor(() => expect(settingsCalls(f, 'POST')).toHaveLength(2));
    expect(settingsCalls(f, 'POST').map((c) => c.body)).toEqual([{ usb_volume: 10 }, { bt_volume: 20 }]);
  });

  it('sends a pending slider move when the panel is closed before the debounce ends', async () => {
    const f = await openPanel();
    fireEvent.change(within(panel()).getByLabelText('USB volume'), { target: { value: '33' } });
    fireEvent.click(within(panel()).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(settingsCalls(f, 'POST')).toHaveLength(1));
    expect(settingsCalls(f, 'POST')[0].body).toEqual({ usb_volume: 33 });
  });

  it('shows an error line when a write fails and re-reads what the pedal has', async () => {
    const f = await openPanel();
    failWrites = true;
    fireEvent.change(within(panel()).getByLabelText('USB volume'), { target: { value: '5' } });
    const alert = await within(panel()).findByRole('alert');
    expect(alert).toHaveTextContent(/could not be changed/i);
    await waitFor(() => expect(settingsCalls(f, 'GET')).toHaveLength(2));
    await waitFor(() => expect(within(panel()).getByLabelText('USB volume')).toHaveValue('80'));
    failWrites = false;
    fireEvent.click(within(panel()).getByLabelText('Loopback'));
    await waitFor(() => expect(within(panel()).queryByRole('alert')).toBeNull());
  });

  it('shows an error line when the settings cannot be read', async () => {
    const f = await renderBridged();
    (f.fn as unknown as ReturnType<typeof vi.fn>).mockImplementationOnce(async () => new Response(JSON.stringify({ error: { code: 'device_timeout', message: 'x' } }), { status: 504 }));
    fireEvent.click(gear());
    expect(await within(panel()).findByRole('alert')).toHaveTextContent(/could not be read/i);
  });

  it('closes with Escape, an outside click and the close button', async () => {
    await openPanel();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: 'Pedal settings' })).toBeNull();
    expect(gear()).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(gear());
    await screen.findByRole('dialog', { name: 'Pedal settings' });
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('dialog', { name: 'Pedal settings' })).toBeNull();

    fireEvent.click(gear());
    fireEvent.click(await within(await screen.findByRole('dialog', { name: 'Pedal settings' })).findByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog', { name: 'Pedal settings' })).toBeNull();
  });

  it('stays open when the click is inside the panel, and the gear toggles it', async () => {
    await openPanel();
    fireEvent.mouseDown(within(panel()).getByLabelText('Loopback'));
    expect(panel()).toBeInTheDocument();
    fireEvent.click(gear());
    expect(screen.queryByRole('dialog', { name: 'Pedal settings' })).toBeNull();
  });

  it('is read-only when the server is read-only: controls disabled, nothing written', async () => {
    const f = await openPanel();
    act(() => useDeviceStore.setState({ readOnly: true }));
    const p = within(panel());
    for (const name of ['Loopback', 'Input gain', 'USB volume', 'Bluetooth volume', 'MIDI channel', 'Bluetooth of the pedal']) {
      expect(p.getByLabelText(name)).toBeDisabled();
    }
    expect(p.getByText(/read-only/i)).toBeInTheDocument();
    expect(settingsCalls(f, 'POST')).toHaveLength(0);
  });

  it('is disabled while the pedal is disconnected, and reads again when it comes back', async () => {
    const f = await openPanel();
    act(() => useDeviceStore.setState({ pedalConnected: false }));
    expect(within(panel()).getByLabelText('Loopback')).toBeDisabled();
    expect(within(panel()).getByText(/not connected/i)).toBeInTheDocument();
    act(() => useDeviceStore.setState({ pedalConnected: true }));
    await waitFor(() => expect(settingsCalls(f, 'GET')).toHaveLength(2));
    await waitFor(() => expect(within(panel()).getByLabelText('Loopback')).toBeEnabled());
  });

  it('does not read the settings of a pedal that is not connected', async () => {
    const f = await renderBridged();
    act(() => useDeviceStore.setState({ pedalConnected: false }));
    fireEvent.click(gear());
    expect(within(panel()).getByLabelText('Loopback')).toBeDisabled();
    expect(settingsCalls(f, 'GET')).toHaveLength(0);
  });
});
