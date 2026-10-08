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

const fileDoc = { format: 'nanocore-controller-backup', version: 1, preset: { slot: 8, name: 'FromDsk' } };
let uploadReply: { status?: number; body: unknown };
let downloadReply: { status?: number; body: unknown; headers?: Record<string, string> };

async function renderBridged() {
  uploadReply = { body: makeState({ rev: 7, preset: { slot: 8, display_number: 9, name: 'FromDsk' } }) };
  downloadReply = { body: fileDoc, headers: { 'Content-Disposition': 'attachment; filename="preset-09-FunkCln.json"' } };
  const f = makeFetch((call: Call) => {
    if (call.path === '/api/presets') return { body: { presets: [{ slot: 8, display_number: 9, name: 'FunkCln' }] } };
    if (call.path === '/api/assets') return { body: { amp: null, ir: null } };
    if (call.path === '/api/preset-file') return call.method === 'GET' ? downloadReply : uploadReply;
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

const downloadButton = () => screen.getByRole('button', { name: 'Download preset file' });
const uploadButton = () => screen.getByRole('button', { name: 'Upload preset file' });
const fileInput = () => document.querySelector<HTMLInputElement>('input[type="file"]')!;
const fileCalls = (f: { calls: Call[] }, method: string) => f.calls.filter((c) => c.path === '/api/preset-file' && c.method === method);
const pick = (content: string, name = 'mine.json') =>
  fireEvent.change(fileInput(), { target: { files: [new File([content], name, { type: 'application/json' })] } });

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
  await usePatchStore.getState().initTransport('test');
  resetBridgeTokenForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('download and upload buttons', () => {
  it('are two more icon buttons in the preset row, the same size as the others, after save and restore', async () => {
    await renderBridged();
    const row = screen.getByRole('combobox', { name: 'Preset' }).closest('.preset-selector')!;
    const buttons = [...row.querySelectorAll('button')];
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual([
      'Rename preset',
      'Save preset',
      'Restore saved version',
      'Download preset file',
      'Upload preset file',
    ]);
    for (const b of buttons) {
      expect(b).toHaveClass('preset-selector__icon');
      expect(b.querySelector('svg')).not.toBeNull();
      expect(b.textContent).toBe('');
    }
  });

  it('are disabled without a pedal, and the upload also in read-only mode', async () => {
    await renderBridged();
    expect(downloadButton()).toBeEnabled();
    expect(uploadButton()).toBeEnabled();
    act(() => useDeviceStore.setState({ readOnly: true }));
    expect(downloadButton()).toBeEnabled();
    expect(uploadButton()).toBeDisabled();
    act(() => useDeviceStore.setState({ readOnly: false, pedalConnected: false }));
    expect(downloadButton()).toBeDisabled();
    expect(uploadButton()).toBeDisabled();
  });
});

describe('downloading the preset', () => {
  it('fetches the file and saves it through a Blob link with the name the server gave', async () => {
    const f = await renderBridged();
    fireEvent.click(downloadButton());
    await waitFor(() => expect(clicked).toHaveLength(1));
    expect(fileCalls(f, 'GET')).toHaveLength(1);
    expect(clicked[0].download).toBe('preset-09-FunkCln.json');
    expect(clicked[0].href).toBe('blob:test-url');
    expect(blobs).toHaveLength(1);
    expect(blobs[0].type).toBe('application/json');
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(blobs[0]);
    });
    expect(JSON.parse(text)).toEqual(fileDoc);
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:test-url'), { timeout: 3000 });
    expect(document.querySelector('a[download]')).toBeNull(); // the temporary link is removed
  });

  it('shows an error banner when the download fails', async () => {
    await renderBridged();
    downloadReply = { status: 503, body: { error: { code: 'disconnected', message: 'no link to the pedal' } } };
    fireEvent.click(downloadButton());
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/preset file/i);
    expect(clicked).toHaveLength(0);
  });
});

describe('uploading a preset file', () => {
  it('opens the file picker for JSON files', async () => {
    await renderBridged();
    const click = vi.spyOn(fileInput(), 'click');
    fireEvent.click(uploadButton());
    expect(click).toHaveBeenCalledTimes(1);
    expect(fileInput().accept).toMatch(/json/);
    expect(fileInput()).toHaveAttribute('hidden');
  });

  it('asks for confirmation, then posts the file and shows the new state (unsaved, nothing stored)', async () => {
    const f = await renderBridged();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    pick(JSON.stringify(fileDoc));
    await waitFor(() => expect(fileCalls(f, 'POST')).toHaveLength(1));
    expect(confirm).toHaveBeenCalledWith(
      'Replace the content of the current preset with this file? It is applied live; press save to keep it.',
    );
    expect(fileCalls(f, 'POST')[0].body).toEqual(fileDoc);
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveDisplayValue('9 · FromDsk'));
    expect(f.calls.some((c) => c.path === '/api/save-now')).toBe(false);
    expect(fileInput().value).toBe(''); // the same file can be chosen again
  });

  it('does nothing when the confirmation is refused', async () => {
    const f = await renderBridged();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    pick(JSON.stringify(fileDoc));
    await new Promise((r) => setTimeout(r, 60));
    expect(fileCalls(f, 'POST')).toHaveLength(0);
  });

  it('shows the server message when the file is refused (400)', async () => {
    const f = await renderBridged();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    uploadReply = { status: 400, body: { error: { code: 'validation', message: 'backup must contain exactly eight effects' } } };
    pick(JSON.stringify(fileDoc));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('backup must contain exactly eight effects');
    expect(fileCalls(f, 'POST')).toHaveLength(1);
    expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveDisplayValue('9 · FunkCln');
  });

  it('shows an error without asking or posting when the file is not JSON', async () => {
    const f = await renderBridged();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    pick('this is not json {');
    expect(await screen.findByRole('alert')).toHaveTextContent(/not a valid preset file|not valid JSON/i);
    expect(confirm).not.toHaveBeenCalled();
    expect(fileCalls(f, 'POST')).toHaveLength(0);
  });

  it('refuses a file that is far bigger than any preset without reading it to the server', async () => {
    const f = await renderBridged();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    pick('{"x":"' + 'a'.repeat(70 * 1024) + '"}');
    expect(await screen.findByRole('alert')).toHaveTextContent(/too large|too big/i);
    expect(confirm).not.toHaveBeenCalled();
    expect(fileCalls(f, 'POST')).toHaveLength(0);
  });

  it('keeps the error out of the edit flow: a network failure is reported as the file not loaded', async () => {
    await renderBridged();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    uploadReply = { status: 504, body: { error: { code: 'device_timeout', message: 'timeout', maybe_applied: true } } };
    pick(JSON.stringify(fileDoc));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/may or may not/i)).toBeInTheDocument();
  });
});
