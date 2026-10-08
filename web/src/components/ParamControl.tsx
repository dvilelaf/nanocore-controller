import { useTranslation } from 'react-i18next';
import type { ParamSpec, RangeParam } from '../data/types';

interface Props {
  spec: ParamSpec;
  value: number;
  onChange: (value: number) => void;
  /** Keeps element ids unique when several blocks are on screen at once. */
  idPrefix?: string;
}

function decimalsFor(spec: RangeParam): number {
  return spec.decimals ?? (Number.isInteger(spec.min) && Number.isInteger(spec.max) ? 0 : 2);
}

function formatValue(spec: ParamSpec, value: number): string {
  if (spec.kind === 'enum') return spec.options[value] ?? String(value);
  const decimals = decimalsFor(spec);
  if (spec.format === 'freq') {
    const abs = Math.abs(value);
    if (abs >= 1000) {
      const kHz = value / 1000;
      return `${kHz % 1 === 0 ? kHz.toFixed(0) : kHz.toFixed(2)} kHz`;
    }
    return `${value.toFixed(0)} Hz`;
  }
  if (spec.format === 'time') {
    return Math.abs(value) >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(decimals)} ms`;
  }
  const num = value.toFixed(decimals);
  if (!spec.unit) return num;
  return spec.unit.startsWith(':') ? `${num}${spec.unit}` : `${num} ${spec.unit}`;
}

export function ParamControl({ spec, value, onChange, idPrefix }: Props) {
  const { t } = useTranslation();
  const inputId = `${idPrefix ? `${idPrefix}-` : ''}param-${spec.id}`;
  const label = t(`param.${spec.id}`, spec.label);

  if (spec.kind === 'enum') {
    return (
      <div className="param-control">
        <div className="param-control__head">
          <label className="param-control__label" htmlFor={inputId}>
            {label}
          </label>
          <span className="param-control__value">{formatValue(spec, value)}</span>
        </div>
        <select
          id={inputId}
          className="param-control__select"
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
        >
          {spec.options.map((opt, i) => (
            <option key={opt} value={i}>
              {t(`enum.${opt.toLowerCase()}`, opt)}
            </option>
          ))}
        </select>
        {spec.note && <p className="param-control__note">{spec.note}</p>}
      </div>
    );
  }

  const step = (spec.max - spec.min) / 127;
  return (
    <div className="param-control">
      <div className="param-control__head">
        <label className="param-control__label" htmlFor={inputId}>
          {label}
        </label>
        <span className="param-control__value">{formatValue(spec, value)}</span>
      </div>
      <input
        id={inputId}
        className="param-control__range"
        type="range"
        min={spec.min}
        max={spec.max}
        step={step || 1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {spec.note && <p className="param-control__note">{spec.note}</p>}
    </div>
  );
}
