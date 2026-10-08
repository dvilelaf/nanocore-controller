import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { ChainBoard } from '../ChainBoard';
import { usePatchStore } from '../../store/patchStore';
import { useDeviceStore } from '../../store/deviceState';
import { activeParams, findBlock } from '../../store/patchDefaults';
import { nanocoreSpec } from '../../data/nanocoreSpec';

const renderBoard = () =>
  render(
    <I18nextProvider i18n={i18n}>
      <ChainBoard />
    </I18nextProvider>,
  );

const cards = (container: HTMLElement) => [...container.querySelectorAll<HTMLElement>('[data-block]')];
const order = (container: HTMLElement) => cards(container).map((c) => c.dataset.block);

beforeEach(async () => {
  await usePatchStore.getState().initTransport('simulator');
  usePatchStore.getState().resetPatch();
  usePatchStore.getState().clearLog();
  useDeviceStore.getState().reset();
});

afterEach(cleanup);

describe('ChainBoard', () => {
  it('renders one card per block in the store chain order', () => {
    usePatchStore.setState({ chainOrder: ['eq', 'rev', 'del', 'mod', 'cab', 'amp', 'fx2', 'fx1'] });
    const { container } = renderBoard();
    expect(order(container)).toEqual(['eq', 'rev', 'del', 'mod', 'cab', 'amp', 'fx2', 'fx1']);
    expect(screen.getAllByRole('listitem')).toHaveLength(8);
  });

  it('has no arrows between the blocks and no move buttons on the cards', () => {
    const { container } = renderBoard();
    expect(container.querySelector('.chain-board__arrow')).toBeNull();
    expect(screen.queryByRole('button', { name: /Move .* in the chain/ })).toBeNull();
    expect(container.querySelector('.block-card__reorder')).toBeNull();
  });

  it('shows name, power, type and every parameter of every block at once', () => {
    const { container } = renderBoard();
    for (const block of nanocoreSpec.blocks) {
      const card = container.querySelector<HTMLElement>(`[data-block="${block.id}"]`)!;
      const typeId = usePatchStore.getState().patch[block.id].typeId;
      expect(within(card).getByRole('heading', { level: 3 })).toHaveTextContent(block.name);
      expect(within(card).getByRole('checkbox', { name: `${block.name} power` })).toBeInTheDocument();
      expect(within(card).getByRole('combobox', { name: `${block.name} type` })).toBeInTheDocument();
      expect(card.querySelectorAll('.param-control')).toHaveLength(activeParams(findBlock(block.id), typeId).length);
    }
  });

  it('keeps element ids unique across cards', () => {
    const { container } = renderBoard();
    const ids = [...container.querySelectorAll('[id]')].map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('exposes the power state as text and as the checkbox state, not by colour alone', () => {
    const { container } = renderBoard();
    const card = container.querySelector<HTMLElement>('[data-block="fx1"]')!;
    const power = within(card).getByRole('checkbox', { name: 'FX1 power' });
    expect(power).not.toBeChecked();
    expect(card).toHaveTextContent('Off');
    fireEvent.click(power);
    expect(power).toBeChecked();
    expect(card).toHaveTextContent('On');
    expect(usePatchStore.getState().patch.fx1.on).toBe(true);
    expect(usePatchStore.getState().log[0]).toMatchObject({ kind: 'cc', cc: 20, value: 127 });
  });

  it('changes the type through the select', () => {
    const { container } = renderBoard();
    const card = container.querySelector<HTMLElement>('[data-block="fx2"]')!;
    fireEvent.change(within(card).getByRole('combobox', { name: 'FX2 type' }), { target: { value: '3' } });
    expect(usePatchStore.getState().patch.fx2.typeId).toBe(3);
  });

  it('reorders by dragging a handle onto another card', () => {
    const { container } = renderBoard();
    fireEvent.dragStart(screen.getByTestId('handle-fx1'));
    const target = container.querySelector('[data-block="eq"]')!;
    fireEvent.dragOver(target);
    fireEvent.drop(target);
    expect(order(container)).toEqual(['fx2', 'amp', 'cab', 'mod', 'del', 'rev', 'eq', 'fx1']);
    expect(usePatchStore.getState().log.filter((m) => m.kind === 'sysex')).toHaveLength(1);
    expect(screen.getByRole('status')).toHaveTextContent('FX1 moved to position 8 of 8');
  });

  it('ignores a drop that did not start from a handle', () => {
    const { container } = renderBoard();
    fireEvent.drop(container.querySelector('[data-block="eq"]')!);
    expect(order(container)[0]).toBe('fx1');
  });

  it('locks every control in read-only mode', () => {
    useDeviceStore.setState({ readOnly: true });
    renderBoard();
    expect(screen.getByRole('checkbox', { name: 'FX1 power' })).toBeDisabled();
    expect(screen.getByTestId('handle-fx1')).not.toHaveAttribute('draggable', 'true');
  });
});

describe('ChainBoard with values from the pedal', () => {
  beforeEach(() => useDeviceStore.setState({ hydrated: true }));

  it('shows no calibration badge when the pedal matches the measured firmware', () => {
    const { container } = renderBoard();
    expect(screen.queryByText('firmware differs from the measured one')).toBeNull();
    expect(container.querySelector('[data-block="amp"]')!.querySelectorAll('.param-control')).toHaveLength(5);
  });

  it('hides the parameters and shows the badge only for a block whose layout differs', () => {
    useDeviceStore.setState({ unaligned: ['del'] });
    const { container } = renderBoard();
    expect(screen.getAllByText('firmware differs from the measured one')).toHaveLength(1);
    const del = container.querySelector<HTMLElement>('[data-block="del"]')!;
    expect(del.querySelectorAll('.param-control')).toHaveLength(0);
    expect(within(del).getByRole('checkbox')).toBeInTheDocument();
    expect(within(del).getByRole('combobox')).toBeInTheDocument();
    expect(container.querySelector('[data-block="mod"]')!.querySelectorAll('.param-control').length).toBeGreaterThan(0);
  });

  it('hides the controls the pedal does not have, such as the Level of MOD, DEL and REV', () => {
    usePatchStore.getState().setBlockType('del', 0); // BBD: Delay, Feedback, Age, Mix
    const { container } = renderBoard();
    const labels = (id: string) =>
      [...container.querySelectorAll(`[data-block="${id}"] .param-control__label`)].map((e) => e.textContent);
    expect(labels('del')).toEqual(['Delay', 'Feedback', 'Age', 'Mix']);
    expect(labels('mod')).not.toContain('Level');
    expect(labels('rev')).not.toContain('Level');
    expect(labels('mod')).toEqual(['Rate', 'Depth']);
    expect(container.textContent).not.toContain('not readable');
  });

  it('keeps every control of the amplifier', () => {
    const { container } = renderBoard();
    const amp = [...container.querySelectorAll('[data-block="amp"] .param-control__label')].map((e) => e.textContent);
    expect(amp).toEqual(['Gain', 'Bass', 'Mid', 'Treble', 'Level']);
  });

  it('shows the amp and cab slot names from the pedal as plain text', () => {
    useDeviceStore.setState({
      assets: { amp: { slot: 12, name: 'MesR2' }, ir: { slot: 2, name: 'Eng412A' } },
    });
    const { container } = renderBoard();
    expect(container.querySelector('[data-block="amp"]')).toHaveTextContent('On the pedal: MesR2');
    expect(container.querySelector('[data-block="cab"]')).toHaveTextContent('On the pedal: Eng412A');
  });
});

describe('ChainBoard value controls', () => {
  beforeEach(async () => {
    await usePatchStore.getState().initTransport('simulator');
    usePatchStore.getState().resetPatch();
    useDeviceStore.getState().reset();
  });
  afterEach(cleanup);

  it('edits numeric parameters with a slider only, with no extra number box', () => {
    const { container } = renderBoard();
    expect(container.querySelectorAll('input[type="range"]').length).toBeGreaterThan(0);
    expect(container.querySelectorAll('input[type="number"]')).toHaveLength(0);
    expect(container.querySelector('.param-control__exact')).toBeNull();
  });

  it('still shows each value with its unit above the slider', () => {
    const { container } = renderBoard();
    expect(container.querySelector('.param-control__value')).not.toBeNull();
  });
});
