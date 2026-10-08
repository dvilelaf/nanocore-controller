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
import type { ModelList } from '../midi/bridgeTransport';

const entry = (slot: number, name: string, active = false) => ({ slot, name, size: 4096, crc32: slot + 1, active });
const initial: ModelList = {
  amp: [entry(11, 'JCM800'), entry(12, 'MesR2', true), entry(30, 'Drive30')],
  ir: [entry(2, 'Eng412A', true), entry(3, 'Vint2x12')],
};

let lists: ModelList;
let uploadReply: { status?: number; body: unknown };
let downloadReply: { status?: number; body: unknown; bytes?: Uint8Array; headers?: Record<string, string> };

async function renderBridged() {
  lists = structuredClone(initial);
  uploadReply = { body: entry(3, 'Vint2x12') };
  downloadReply = {
    body: null,
    bytes: new Uint8Array([68, 68, 80, 66]),
    headers: { 'Content-Disposition': 'attachment; filename="amp-12-MesR2.bin"' },
  };
  const f = makeFetch((call: Call) => {
    if (call.path === '/api/presets') return { body: { presets: [{ slot: 8, display_number: 9, name: 'FunkCln' }] } };
    if (call.path === '/api/assets') return { body: { amp: { slot: 12, name: 'MesR2' }, ir: { slot: 2, name: 'Eng412A' } } };
    if (call.path === '/api/models') return { body: lists };
    if (call.path.startsWith('/api/models/')) return call.method === 'GET' ? downloadReply : uploadReply;
    if (call.path === '/api/settings') return { body: { version: 1, wireless_enabled: true, loopback_enabled: false, input_gain_db: 0, usb_volume: 1, bt_volume: 1, midi_channel: 0, volume_floor: null, volume_ceiling: null } };
    return { body: makeState() };
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

const fab = () => screen.getByRole('button', { name: 'Models' });
const panel = () => screen.getByRole('dialog', { name: 'Amplifier and cabinet models' });
const modelCalls = (f: { calls: Call[] }, method: string, path = '/api/models') =>
  f.calls.filter((c) => c.path === path && c.method === method);
const fileInput = () => panel().querySelector<HTMLInputElement>('input[type="file"]')!;

async function openPanel() {
  const f = await renderBridged();
  fireEvent.click(fab());
  await within(panel()).findByText('MesR2');
  return f;
}

let blobs: Blob[];
let clicked: { download: string; href: string }[];

beforeEach(() => {
  FakeWebSocket.reset();
  useDeviceStore.getState().reset();
  blobs = [];
  clicked = [];
  URL.createObjectURL = vi.fn((blob: Blob) => {
    blobs.push(blob);
    return 'blob:test-url';
  });
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push({ download: this.download, href: this.href });
  });
});

afterEach(async () => {
  cleanup();
  await usePatchStore.getState().initTransport('simulator');
  resetBridgeTokenForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('models button', () => {
  it('is a floating icon button beside the tuner and the settings gear, with the same size class', async () => {
    await renderBridged();
    expect(fab().textContent).toBe('');
    expect(fab().querySelector('svg')).not.toBeNull();
    expect(fab()).toHaveClass('fab', 'models-fab');
    expect(screen.getByRole('button', { name: 'Settings' })).toHaveClass('fab', 'settings-fab');
    expect(screen.getByRole('button', { name: 'Tuner' })).toHaveClass('fab', 'tuner-fab');
    expect(fab()).toHaveAttribute('aria-expanded', 'false');
  });

  it('is not offered outside the server mode', async () => {
    render(
      <I18nextProvider i18n={i18n}>
        <App />
      </I18nextProvider>,
    );
    await waitFor(() => expect(usePatchStore.getState().connection.ready).toBe(true));
    expect(screen.queryByRole('button', { name: 'Models' })).toBeNull();
  });

  it('keeps the panel closed and reads nothing until it is used', async () => {
    const f = await renderBridged();
    expect(screen.queryByRole('dialog', { name: 'Amplifier and cabinet models' })).toBeNull();
    expect(modelCalls(f, 'GET')).toHaveLength(0);
  });
});

describe('models panel', () => {
  it('lists the amplifiers with their slot, name and the active one marked', async () => {
    const f = await openPanel();
    expect(modelCalls(f, 'GET')).toHaveLength(1);
    expect(fab()).toHaveAttribute('aria-expanded', 'true');
    const rows = within(panel()).getAllByRole('listitem');
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('JCM800'),
      expect.stringContaining('MesR2'),
      expect.stringContaining('Drive30'),
    ]);
    expect(rows[1]).toHaveTextContent('12');
    expect(rows[1]).toHaveTextContent('Active');
    expect(rows[0]).not.toHaveTextContent('Active');
  });

  it('has two tabs, Amplifiers first, and switching shows the cabinets without reading again', async () => {
    const f = await openPanel();
    const tabs = within(panel()).getAllByRole('tab');
    expect(tabs.map((tab) => tab.textContent)).toEqual(['Amplifiers', 'Cabinets (IR)']);
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(tabs[1]);
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true');
    expect(within(panel()).getByText('Eng412A')).toBeInTheDocument();
    expect(within(panel()).queryByText('MesR2')).toBeNull();
    expect(modelCalls(f, 'GET')).toHaveLength(1);
  });

  it('brings the active slot into view inside the scrolling list, also after a tab change', async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    await openPanel();
    await waitFor(() => expect(scrolled).toHaveBeenCalled());
    expect((scrolled.mock.contexts.at(-1) as HTMLElement).textContent).toContain('MesR2');
    expect(scrolled).toHaveBeenLastCalledWith({ block: 'nearest' });
    fireEvent.click(within(panel()).getByRole('tab', { name: 'Cabinets (IR)' }));
    await waitFor(() => expect((scrolled.mock.contexts.at(-1) as HTMLElement).textContent).toContain('Eng412A'));
  });

  it('moves between the tabs with the arrow keys', async () => {
    await openPanel();
    const [amps, irs] = within(panel()).getAllByRole('tab');
    amps.focus();
    fireEvent.keyDown(amps, { key: 'ArrowRight' });
    expect(irs).toHaveAttribute('aria-selected', 'true');
    expect(irs).toHaveFocus();
    fireEvent.keyDown(irs, { key: 'ArrowLeft' });
    expect(amps).toHaveAttribute('aria-selected', 'true');
  });

  it('explains the drive models and the .ead files on the amplifier tab only', async () => {
    await openPanel();
    expect(within(panel()).getByText('Amplifiers 30 to 37 are drive models')).toBeInTheDocument();
    expect(
      within(panel()).getByText(
        "Models read from a pedal can be written here. Livtra's encrypted .ead files need a decryptor that you provide (docs/ead-decryptor.md); none is configured on this server.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(within(panel()).getByRole('tab', { name: 'Cabinets (IR)' }));
    expect(within(panel()).queryByText(/drive models/)).toBeNull();
    expect(within(panel()).queryByText(/\.ead/)).toBeNull();
  });

  it('closes with Escape, an outside click and the close button', async () => {
    await openPanel();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: 'Amplifier and cabinet models' })).toBeNull();
    expect(fab()).toHaveFocus();
    fireEvent.click(fab());
    await within(panel()).findByText('MesR2');
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('dialog', { name: 'Amplifier and cabinet models' })).toBeNull();
    fireEvent.click(fab());
    await within(panel()).findByText('MesR2');
    fireEvent.click(within(panel()).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog', { name: 'Amplifier and cabinet models' })).toBeNull();
  });

  it('is never open together with the settings panel', async () => {
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    expect(screen.getByRole('dialog', { name: 'Pedal settings' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: 'Amplifier and cabinet models' })).toBeNull();
    fireEvent.click(fab());
    expect(screen.queryByRole('dialog', { name: 'Pedal settings' })).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Amplifier and cabinet models' })).toBeInTheDocument();
  });

  it('shows a small error line when the list cannot be read', async () => {
    await renderBridged();
    lists = undefined as unknown as ModelList;
    const failing = vi.fn(async () => new Response(JSON.stringify({ error: { code: 'disconnected', message: 'no link to the pedal' } }), { status: 503 }));
    vi.stubGlobal('fetch', failing);
    fireEvent.click(fab());
    expect(await within(panel()).findByRole('alert')).toHaveTextContent('The models could not be read from the pedal.');
  });
});

describe('download', () => {
  it('saves the verified file through a Blob link with the name the server proposes', async () => {
    const f = await openPanel();
    fireEvent.click(within(panel()).getByRole('button', { name: 'Download slot 12 (MesR2)' }));
    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(modelCalls(f, 'GET', '/api/models/amp/12')).toHaveLength(1);
    expect(clicked[0]).toEqual({ download: 'amp-12-MesR2.bin', href: 'blob:test-url' });
    expect(new Uint8Array(await blobs[0].arrayBuffer())).toEqual(new Uint8Array([68, 68, 80, 66]));
  });

  it('downloads a cabinet from the IR storage', async () => {
    const f = await openPanel();
    fireEvent.click(within(panel()).getByRole('tab', { name: 'Cabinets (IR)' }));
    downloadReply = { body: null, bytes: new Uint8Array([1, 2, 3, 4]), headers: { 'Content-Disposition': 'attachment; filename="ir-3-Vint2x12.bin"' } };
    fireEvent.click(within(panel()).getByRole('button', { name: 'Download slot 3 (Vint2x12)' }));
    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(modelCalls(f, 'GET', '/api/models/ir/3')).toHaveLength(1);
  });

  it('shows the server message when the download fails', async () => {
    await openPanel();
    downloadReply = { status: 502, body: { error: { code: 'protocol', message: 'amp slot 12 does not match its checksum' } } };
    fireEvent.click(within(panel()).getByRole('button', { name: 'Download slot 12 (MesR2)' }));
    expect(await within(panel()).findByRole('alert')).toHaveTextContent('amp slot 12 does not match its checksum');
    expect(clicked).toHaveLength(0);
  });
});

describe('upload', () => {
  const pick = (name: string, content: BlobPart = new Uint8Array([1, 2, 3, 4])) =>
    fireEvent.change(fileInput(), { target: { files: [new File([content], name)] } });

  it('offers .bin and .ead for amplifiers and .wav and .bin for cabinets', async () => {
    await openPanel();
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 12 (MesR2)' }));
    expect(fileInput().accept).toBe('.bin,.ead,.eadl');
    fireEvent.click(within(panel()).getByRole('tab', { name: 'Cabinets (IR)' }));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 3 (Vint2x12)' }));
    expect(fileInput().accept).toBe('.wav,.bin');
  });

  it('says that .ead files are installed through the decryptor when the server has one', async () => {
    await renderBridged();
    lists.ead = true;
    fireEvent.click(fab());
    await within(panel()).findByText('MesR2');
    expect(within(panel()).getByText("Models read from a pedal can be written here, and Livtra's .ead files through your decryptor.")).toBeInTheDocument();
    expect(within(panel()).queryByText(/none is configured/)).toBeNull();
  });

  it('sends an .ead file to the amplifier slot like any other file; the server decides', async () => {
    const f = await openPanel();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 12 (MesR2)' }));
    pick('tone.ead', new Uint8Array([83, 65, 80, 70]));
    await waitFor(() => expect(f.calls.some((c: Call) => c.method === 'POST' && c.path === '/api/models/amp/12')).toBe(true));
  });

  it('asks first, then sends the raw file to the slot and reads the list again', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const f = await openPanel();
    fireEvent.click(within(panel()).getByRole('tab', { name: 'Cabinets (IR)' }));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 3 (Vint2x12)' }));
    lists.ir[1] = entry(3, 'Mine');
    pick('mine.wav');
    await waitFor(() => expect(modelCalls(f, 'POST', '/api/models/ir/3')).toHaveLength(1));
    expect(confirm).toHaveBeenCalledWith(
      "Replace 'Vint2x12' in slot 3? The old content is kept in a backup on the computer running the server.",
    );
    const post = modelCalls(f, 'POST', '/api/models/ir/3')[0];
    expect(post.body).toBeInstanceOf(File);
    expect(post.headers['Content-Type']).toBe('application/octet-stream');
    await waitFor(() => expect(modelCalls(f, 'GET')).toHaveLength(2));
    expect(await within(panel()).findByText('Mine')).toBeInTheDocument();
    expect(within(panel()).queryByRole('alert')).toBeNull();
  });

  it('sends nothing when the question is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const f = await openPanel();
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 12 (MesR2)' }));
    pick('amp.bin');
    expect(modelCalls(f, 'POST', '/api/models/amp/12')).toHaveLength(0);
    expect(modelCalls(f, 'GET')).toHaveLength(1);
  });

  it('shows the server message when the file is refused, and keeps the list', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await openPanel();
    uploadReply = { status: 400, body: { error: { code: 'validation', message: 'an amplifier blob read from the pedal starts with DDPB' } } };
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 12 (MesR2)' }));
    pick('amp.ead');
    expect(await within(panel()).findByRole('alert')).toHaveTextContent('an amplifier blob read from the pedal starts with DDPB');
    expect(within(panel()).getByText('MesR2')).toBeInTheDocument();
  });

  it('refuses a file over 4 MiB without asking the server', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const f = await openPanel();
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 12 (MesR2)' }));
    pick('big.bin', new Uint8Array(4 * 1024 * 1024 + 1));
    expect(await within(panel()).findByRole('alert')).toHaveTextContent('4 MiB');
    expect(confirm).not.toHaveBeenCalled();
    expect(modelCalls(f, 'POST', '/api/models/amp/12')).toHaveLength(0);
  });

  it('refreshes the name shown on the block card after writing the active slot', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await openPanel();
    expect(useDeviceStore.getState().assets?.amp?.name).toBe('MesR2');
    lists.amp[1] = entry(12, 'Renamed', true);
    const fetched = vi.mocked(fetch);
    fetched.mockImplementation(async (path: RequestInfo | URL, init?: RequestInit) => {
      if (String(path) === '/api/assets') return new Response(JSON.stringify({ amp: { slot: 12, name: 'Renamed' }, ir: { slot: 2, name: 'Eng412A' } }));
      if (String(path) === '/api/models') return new Response(JSON.stringify(lists));
      if (init?.method === 'POST') return new Response(JSON.stringify(entry(12, 'Renamed', true)));
      return new Response(JSON.stringify(makeState()));
    });
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 12 (MesR2)' }));
    pick('amp.bin');
    await waitFor(() => expect(useDeviceStore.getState().assets?.amp?.name).toBe('Renamed'));
  });

  it('disables everything while a write is running', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await openPanel();
    let finish: (r: Response) => void = () => {};
    vi.mocked(fetch).mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    fireEvent.click(within(panel()).getByRole('button', { name: 'Upload to slot 12 (MesR2)' }));
    pick('amp.bin');
    await waitFor(() => expect(within(panel()).getByRole('button', { name: 'Upload to slot 11 (JCM800)' })).toBeDisabled());
    expect(within(panel()).getByText('Writing slot 12…')).toBeInTheDocument();
    finish(new Response(JSON.stringify(entry(12, 'MesR2', true))));
  });
});

describe('locked states', () => {
  it('disables the uploads, not the downloads, when the server is read-only', async () => {
    await renderBridged();
    act(() => useDeviceStore.setState({ readOnly: true }));
    fireEvent.click(fab());
    await within(panel()).findByText('MesR2');
    for (const b of within(panel()).getAllByRole('button', { name: /^Upload to slot/ })) expect(b).toBeDisabled();
    for (const b of within(panel()).getAllByRole('button', { name: /^Download slot/ })) expect(b).toBeEnabled();
  });

  it('shows a note and disables the buttons when the pedal is not connected', async () => {
    const f = await renderBridged();
    act(() => useDeviceStore.setState({ pedalConnected: false }));
    fireEvent.click(fab());
    expect(await within(panel()).findByText('The pedal is not connected.')).toBeInTheDocument();
    expect(modelCalls(f, 'GET')).toHaveLength(0);
    expect(within(panel()).queryAllByRole('button', { name: /^(Download|Upload)/ })).toHaveLength(0);
  });

  it('disables the buttons of the list read before the pedal went away', async () => {
    await openPanel();
    act(() => useDeviceStore.setState({ pedalConnected: false }));
    for (const b of within(panel()).getAllByRole('button', { name: /^(Download|Upload)/ })) expect(b).toBeDisabled();
  });
});
