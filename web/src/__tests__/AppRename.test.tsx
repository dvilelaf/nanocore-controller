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

async function renderBridged() {
  const doc = makeState();
  const f = makeFetch((call: Call) => {
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
    if (call.path === '/api/edit') return { status: 202, body: { applied: 1, rev: 2 } };
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
  const select = await screen.findByRole('combobox', { name: 'Preset' });
  await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(2));
  act(() => useDeviceStore.setState({ link: 'connected', pedalConnected: true }));
  return f;
}

const renameButton = () => screen.getByRole('button', { name: 'Rename preset' });
const nameInput = () => screen.getByRole('textbox', { name: 'Preset name' });
const nameEdits = (f: { calls: Call[] }) =>
  f.calls
    .filter((c) => c.method === 'POST' && c.path === '/api/edit')
    .flatMap((c) => (c.body as { ops: { op: string; name?: string }[] }).ops)
    .filter((o) => o.op === 'name');

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

describe('renaming the preset', () => {
  it('puts a pencil icon button in the preset row, right of the dropdown and before save and restore', async () => {
    await renderBridged();
    const row = screen.getByRole('combobox', { name: 'Preset' }).closest('.preset-selector')!;
    expect(row.contains(renameButton())).toBe(true);
    expect(renameButton().querySelector('svg')).not.toBeNull();
    expect(renameButton().textContent).toBe('');
    expect(renameButton()).toHaveClass('preset-selector__icon');
    const order = [...row.querySelectorAll('select, button')].map((e) => e.getAttribute('aria-label') ?? e.tagName);
    expect(order.slice(0, 4)).toEqual(['SELECT', 'Rename preset', 'Save preset', 'Restore saved version']);
  });

  it('turns the dropdown into a text input with the current name, at most 8 characters', async () => {
    await renderBridged();
    fireEvent.click(renameButton());
    expect(screen.queryByRole('combobox', { name: 'Preset' })).toBeNull();
    expect(nameInput()).toHaveValue('FunkCln');
    expect(nameInput()).toHaveFocus();
  });

  it('keeps only printable ASCII and at most 8 characters while typing', async () => {
    await renderBridged();
    fireEvent.click(renameButton());
    fireEvent.change(nameInput(), { target: { value: 'Café ☃\tRock!! n roll' } });
    expect(nameInput()).toHaveValue('Caf Rock');
    fireEvent.change(nameInput(), { target: { value: 'A-b_c 1!xyz' } });
    expect(nameInput()).toHaveValue('A-b_c 1!');
    // The 8 characters are counted after the others are removed, also for pasted text.
    fireEvent.change(nameInput(), { target: { value: 'Café Rock 12345' } });
    expect(nameInput()).toHaveValue('Caf Rock');
  });

  it('applies the name with Enter as a live name edit and shows it at once', async () => {
    const f = await renderBridged();
    fireEvent.click(renameButton());
    fireEvent.change(nameInput(), { target: { value: 'Rock' } });
    fireEvent.keyDown(nameInput(), { key: 'Enter' });
    await waitFor(() => expect(nameEdits(f)).toEqual([{ op: 'name', name: 'Rock' }]));
    expect(screen.queryByRole('textbox', { name: 'Preset name' })).toBeNull();
    expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveDisplayValue('9 · Rock');
  });

  it('applies the name with the confirm button, without trailing spaces', async () => {
    const f = await renderBridged();
    fireEvent.click(renameButton());
    fireEvent.change(nameInput(), { target: { value: 'Ab  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply name' }));
    await waitFor(() => expect(nameEdits(f)).toEqual([{ op: 'name', name: 'Ab' }]));
  });

  it('cancels with Escape and with the cancel button, sending nothing', async () => {
    const f = await renderBridged();
    fireEvent.click(renameButton());
    fireEvent.change(nameInput(), { target: { value: 'Rock' } });
    fireEvent.keyDown(nameInput(), { key: 'Escape' });
    expect(screen.queryByRole('textbox', { name: 'Preset name' })).toBeNull();
    expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveDisplayValue('9 · FunkCln');

    fireEvent.click(renameButton());
    expect(nameInput()).toHaveValue('FunkCln');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel rename' }));
    expect(screen.getByRole('combobox', { name: 'Preset' })).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 60));
    expect(nameEdits(f)).toHaveLength(0);
  });

  it('does not accept an empty name, and sends nothing for an unchanged one', async () => {
    const f = await renderBridged();
    fireEvent.click(renameButton());
    fireEvent.change(nameInput(), { target: { value: '   ' } });
    expect(screen.getByRole('button', { name: 'Apply name' })).toBeDisabled();
    fireEvent.keyDown(nameInput(), { key: 'Enter' });
    expect(nameInput()).toBeInTheDocument(); // still editing

    fireEvent.change(nameInput(), { target: { value: 'FunkCln' } });
    fireEvent.keyDown(nameInput(), { key: 'Enter' });
    expect(screen.getByRole('combobox', { name: 'Preset' })).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 60));
    expect(nameEdits(f)).toHaveLength(0);
  });

  it('is disabled in read-only mode and without a pedal', async () => {
    await renderBridged();
    act(() => useDeviceStore.setState({ readOnly: true }));
    expect(renameButton()).toBeDisabled();
    act(() => useDeviceStore.setState({ readOnly: false, pedalConnected: false }));
    expect(renameButton()).toBeDisabled();
  });

  it('stops renaming when the pedal changes to another preset', async () => {
    await renderBridged();
    fireEvent.click(renameButton());
    act(() => useDeviceStore.getState().applyState(makeState({ preset: { slot: 9, display_number: 10, name: 'Tremolo' } })));
    expect(screen.queryByRole('textbox', { name: 'Preset name' })).toBeNull();
    expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveDisplayValue('10 · Tremolo');
  });

  it('leaves the preset unsaved once the server says so, and the save button stores it', async () => {
    const f = await renderBridged();
    const save = screen.getByRole('button', { name: 'Save preset' });
    expect(save).toBeDisabled();
    fireEvent.click(renameButton());
    fireEvent.change(nameInput(), { target: { value: 'Rock' } });
    fireEvent.keyDown(nameInput(), { key: 'Enter' });
    await waitFor(() => expect(nameEdits(f)).toHaveLength(1));
    act(() => FakeWebSocket.last().receive({ type: 'autosave', state: 'dirty', detail: null }));
    expect(screen.getByRole('button', { name: 'Save preset' })).toBeEnabled();
    expect(screen.getByTestId('autosave-status')).toHaveTextContent('Unsaved changes');
    fireEvent.click(screen.getByRole('button', { name: 'Save preset' }));
    await waitFor(() => expect(f.calls.some((c) => c.method === 'POST' && c.path === '/api/save-now')).toBe(true));
  });

  it('shows the name another page set, when the server tells it with a patch', async () => {
    await renderBridged();
    const ws = FakeWebSocket.last();
    act(() => ws.receive({ type: 'state', rev: 1, state: makeState() }));
    act(() => ws.receive({ type: 'patch', rev: 2, ops: [{ op: 'name', name: 'Other' }] }));
    expect(screen.getByRole('combobox', { name: 'Preset' })).toHaveDisplayValue('9 · Other');
  });
});
