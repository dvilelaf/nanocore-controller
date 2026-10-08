import { useState } from 'react';
import type { DragEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { findBlock } from '../store/patchDefaults';
import { usePatchStore } from '../store/patchStore';
import { useDeviceStore } from '../store/deviceState';
import { BlockCard } from './BlockCard';
import { PresetSelector } from './PresetSelector';

export function ChainBoard() {
  const { t } = useTranslation();
  const chainOrder = usePatchStore((s) => s.chainOrder);
  const moveBlockTo = usePatchStore((s) => s.moveBlockTo);
  const readOnly = useDeviceStore((s) => s.readOnly);
  const isBridge = usePatchStore((s) => s.connection.transportKind === 'bridge');
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState('');

  const nameOf = (id: string) => t(`block.${id}.name`, findBlock(id).name);

  const announce = (id: string, index: number) =>
    setAnnouncement(
      t('chain.moved', '{{name}} moved to position {{position}} of {{total}}', {
        name: nameOf(id),
        position: index + 1,
        total: chainOrder.length,
      }),
    );

  const dndFor = (id: string) =>
    readOnly
      ? undefined
      : {
          dragging: dragId === id,
          over: overId === id && dragId !== id,
          onHandleDragStart: (e: DragEvent) => {
            e.dataTransfer?.setData('text/plain', id);
            if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
            setDragId(id);
          },
          onHandleDragEnd: () => {
            setDragId(null);
            setOverId(null);
          },
          onDragOver: (e: DragEvent) => {
            if (!dragId) return;
            e.preventDefault();
            setOverId(id);
          },
          onDrop: (e: DragEvent) => {
            if (!dragId) return;
            e.preventDefault();
            const to = chainOrder.indexOf(id);
            moveBlockTo(dragId, to);
            announce(dragId, to);
            setDragId(null);
            setOverId(null);
          },
        };

  return (
    <section className="chain-board" aria-labelledby="chain-board-title">
      <h2 id="chain-board-title" className="visually-hidden">
        {t('chain.title', 'Effect Chain')}
      </h2>
      {isBridge && <PresetSelector />}
      <fieldset className="chain-board__fieldset" disabled={readOnly}>
        <ol className="chain-board__grid">
          {chainOrder.map((id) => (
            <li key={id} className="chain-board__cell">
              <BlockCard
                blockId={id}
                dnd={dndFor(id)}
              />
            </li>
          ))}
        </ol>
      </fieldset>
      <p className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </p>
    </section>
  );
}
