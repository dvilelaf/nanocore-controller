import type { DragEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { readableParamIds } from '../data/deviceMapping';
import { usePatchStore } from '../store/patchStore';
import { useDeviceStore } from '../store/deviceState';
import { activeParams, findBlock, findType } from '../store/patchDefaults';
import { ParamControl } from './ParamControl';
import { RoutingWarning } from './RoutingWarning';

export interface BlockCardDnd {
  dragging: boolean;
  over: boolean;
  onHandleDragStart: (e: DragEvent) => void;
  onHandleDragEnd: () => void;
  onDragOver: (e: DragEvent) => void;
  onDrop: (e: DragEvent) => void;
}

interface Props {
  blockId: string;
  dnd?: BlockCardDnd;
}

export function BlockCard({ blockId, dnd }: Props) {
  const { t } = useTranslation();
  const block = findBlock(blockId);
  const blockState = usePatchStore((s) => s.patch[blockId]);
  const setBlockOn = usePatchStore((s) => s.setBlockOn);
  const setBlockType = usePatchStore((s) => s.setBlockType);
  const setParam = usePatchStore((s) => s.setParam);
  const hydrated = useDeviceStore((s) => s.hydrated);
  const unaligned = useDeviceStore((s) => s.unaligned);
  const assetName = useDeviceStore((s) =>
    blockId === 'amp' ? s.assets?.amp?.name : blockId === 'cab' ? s.assets?.ir?.name : undefined,
  );

  if (!blockState) return null;

  const type = findType(block, blockState.typeId);
  const allParams = activeParams(block, blockState.typeId);
  const name = t(`block.${block.id}.name`, block.name);
  const paramsHidden = hydrated && unaligned.includes(block.id);
  const readable = hydrated && !paramsHidden ? readableParamIds(block.id, blockState.typeId) : null;
  // Connected, only the controls the pedal really has are shown (the editor's own list has a few
  // more, such as a Level on MOD, DEL and REV that the pedal does not report).
  const params = readable ? allParams.filter((spec) => readable.has(spec.id)) : allParams;
  const titleId = `card-title-${block.id}`;

  const classes = ['block-card'];
  if (blockState.on) classes.push('block-card--on');
  if (dnd?.dragging) classes.push('block-card--dragging');
  if (dnd?.over) classes.push('block-card--over');

  return (
    <article
      className={classes.join(' ')}
      aria-labelledby={titleId}
      data-block={block.id}
      onDragOver={dnd?.onDragOver}
      onDrop={dnd?.onDrop}
    >
      <header className="block-card__header">
        <span
          className="block-card__handle"
          draggable={!!dnd}
          title={t('chain.dragHandle', 'Drag to reorder')}
          aria-hidden
          data-testid={`handle-${block.id}`}
          onDragStart={dnd?.onHandleDragStart}
          onDragEnd={dnd?.onHandleDragEnd}
        >
          ⠿
        </span>
        <h3 id={titleId} className="block-card__title">
          {name}
        </h3>
        <label className="block-card__power">
          <span>{t('block.power', 'Power')}</span>
          <input
            type="checkbox"
            className="visually-hidden"
            aria-label={t('block.powerOf', '{{name}} power', { name })}
            checked={blockState.on}
            onChange={(e) => setBlockOn(block.id, e.target.checked)}
          />
          <span className={`pill ${blockState.on ? 'pill--on' : 'pill--off'}`} aria-hidden>
            {blockState.on ? `● ${t('block.on', 'On')}` : `○ ${t('block.off', 'Off')}`}
          </span>
        </label>
      </header>

      <div className="block-card__type-row">
        <label className="block-card__type-label" htmlFor={`type-${block.id}`}>
          {t('block.type', 'Type')}
        </label>
        <select
          id={`type-${block.id}`}
          className="block-card__type-select"
          aria-label={t('block.typeOf', '{{name}} type', { name })}
          value={blockState.typeId}
          onChange={(e) => setBlockType(block.id, Number(e.target.value))}
        >
          {block.types.map((ty) => (
            <option key={ty.id} value={ty.id}>
              {t(`type.${block.id}.${ty.slug}.name`, ty.name)}
            </option>
          ))}
        </select>
      </div>

      {assetName && (
        <p className="block-card__asset">
          {t('block.onPedal', 'On the pedal:')} <span className="block-card__asset-name">{assetName}</span>
        </p>
      )}

      {paramsHidden && (
        <p className="badge badge--uncalibrated" title={t('calibration.hint', 'The pedal reports a different parameter layout than the one this editor was measured against.')}>
          <span aria-hidden>△ </span>
          {t('calibration.badge', 'firmware differs from the measured one')}
        </p>
      )}

      {(block.warning || type.warning) && (
        <p className="block-card__warning" role="alert">
          {block.warning ?? type.warning}
        </p>
      )}

      {type.description && (
        <p className="block-card__description">{t(`type.${block.id}.${type.slug}.description`, type.description)}</p>
      )}

      {block.id === 'mod' && <RoutingWarning />}

      {paramsHidden ? (
        <p className="block-card__no-params">
          {t('calibration.hidden', 'Parameter values are hidden because this pedal reports a different layout than the measured firmware.')}
        </p>
      ) : params.length === 0 ? (
        !type.warning && (
          <p className="block-card__no-params">
            {t('block.noParams', 'This effect type has no user-adjustable parameters — it self-adjusts automatically.')}
          </p>
        )
      ) : (
        <div className="block-card__params">
          {params.map((spec) => (
            <ParamControl
              key={spec.id}
              idPrefix={block.id}
              spec={spec}
              value={blockState.params[spec.id] ?? (spec.kind === 'range' ? spec.min : 0)}
              onChange={(v) => setParam(block.id, spec.id, v)}
            />
          ))}
        </div>
      )}
    </article>
  );
}
