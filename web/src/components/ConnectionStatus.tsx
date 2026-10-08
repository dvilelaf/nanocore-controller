import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import type { BridgeErrorInfo } from '../midi/bridgeTransport';
import { useDeviceStore } from '../store/deviceState';
import { usePatchStore } from '../store/patchStore';

/** Header indicator for read-only mode; the link itself has the Connection button. */
export function ConnectionStatus() {
  const { t } = useTranslation();
  const isBridge = usePatchStore((s) => s.connection.transportKind === 'bridge');
  const readOnly = useDeviceStore((s) => s.readOnly);

  if (!isBridge || !readOnly) return null;
  return (
    <div className="conn-status">
      <p className="conn-status__item" role="status" data-testid="read-only-status">
        {t('status.readOnly', 'Read-only')}
      </p>
    </div>
  );
}

/** Says when the preset has changes that are not stored, or while it is storing or failed to. */
export function AutosaveBadge() {
  const { t } = useTranslation();
  const autosave = useDeviceStore((s) => s.autosave);

  if (!autosave || autosave.state === 'saved') return null;

  let glyph = '●';
  let text = t('autosave.dirty', 'Unsaved changes');
  if (autosave.state === 'saving') {
    glyph = '◌';
    text = t('autosave.saving', 'Saving…');
  } else if (autosave.state === 'error') {
    glyph = '⚠';
    text = t('autosave.error', 'Problem saving');
  }

  return (
    <p
      className={`conn-status__item conn-status__item--save-${autosave.state}`}
      role="status"
      data-testid="autosave-status"
    >
      <span aria-hidden>{glyph} </span>
      {text}
      {autosave.state === 'error' && autosave.error && (
        <span className="conn-status__detail">
          {': '}
          {autosave.error}
        </span>
      )}
    </p>
  );
}

type Translate = ReturnType<typeof useTranslation>['t'];

/** A problem with a preset file: the server's own words for a refused file, the usual text for anything else. */
function fileMessage(t: Translate, e: BridgeErrorInfo): string {
  let detail: string;
  if (e.code === 'invalid_file') {
    detail = t('bridge.error.invalidFile', 'The file is not valid JSON, so it is not a preset file.');
  } else if (e.code === 'file_too_large') {
    detail = t('bridge.error.fileTooLarge', 'The file is too large to be a preset file.');
  } else if ((e.code === 'validation' || e.code === 'bad_request') && e.message) {
    detail = e.message; // plain text from the server: why the file is not a valid preset
  } else {
    detail = messageFor(t, { ...e, context: 'edit' });
  }
  return t('bridge.error.file', 'Preset file: {{detail}}', { detail });
}

function messageFor(t: Translate, e: BridgeErrorInfo): string {
  if (e.context === 'file') return fileMessage(t, e);
  switch (e.code) {
    case 'unauthorized':
    case 'no_token':
      return t(
        'bridge.error.unauthorized',
        'The server rejected the access token. Reload the page using the address printed by nanocore serve.',
      );
    case 'read_only':
      return t('bridge.error.readOnly', 'The server is in read-only mode. Your change was not sent.');
    case 'forbidden':
      return t('bridge.error.forbidden', 'The server refused the request (host or origin check).');
    case 'unsaved_changes':
      return t('bridge.error.unsavedChanges', 'This preset has unsaved changes. Save or restore it first.');
    case 'slot_changed':
      return t(
        'bridge.error.slotChanged',
        'The active preset changed on the pedal. Your change was not applied; the editor now shows the current preset.',
      );
    case 'rate_limited':
      return e.lost
        ? t('bridge.error.rateLimitedLost', 'Too many edits at once. The last change was not applied.')
        : t('bridge.error.rateLimited', 'Too many edits at once. Retrying…');
    case 'disconnected':
      return t('bridge.error.disconnected', 'The pedal is not connected. Your change was not applied.');
    case 'device_timeout':
      return e.maybeApplied
        ? t(
            'bridge.error.timeoutMaybe',
            'The pedal did not answer. The change may or may not have been applied; the editor shows what the pedal reports.',
          )
        : t('bridge.error.timeout', 'The pedal did not answer. Your change was not applied.');
    case 'device_status':
      return t('bridge.error.deviceStatus', 'The pedal refused the change.');
    case 'validation':
    case 'bad_request':
      return t('bridge.error.invalid', 'The server rejected the change as invalid.');
    case 'too_large':
      return t('bridge.error.tooLarge', 'The request was too large for the server.');
    case 'network':
      return t('bridge.error.network', 'The editor lost contact with the server.');
    default:
      return t('bridge.error.unknown', 'The server reported an error ({{code}}).', { code: e.code });
  }
}

export function BridgeErrorBanner() {
  const { t } = useTranslation();
  const error = useDeviceStore((s) => s.error);
  const setError = useDeviceStore((s) => s.setError);

  useEffect(() => {
    if (!error || error.lost) return;
    const id = window.setTimeout(() => setError(null), 4000);
    return () => window.clearTimeout(id);
  }, [error, setError]);

  if (!error) return null;
  return (
    <div className="app-notice app-notice--error" role="alert">
      <span>
        <span aria-hidden>⚠ </span>
        {messageFor(t, error)}
      </span>
      <button type="button" className="btn btn--ghost btn--small" onClick={() => setError(null)}>
        {t('bridge.error.dismiss', 'Dismiss')}
      </button>
    </div>
  );
}
