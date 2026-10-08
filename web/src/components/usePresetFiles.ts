import { useTranslation } from 'react-i18next';
import type { ChangeEvent } from 'react';
import { BridgeApiError, bridgeTransport } from '../midi/bridgeTransport';

/** The server accepts bodies up to 64 KiB; a preset file is a few KiB. */
const MAX_FILE_BYTES = 64 * 1024;

function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new BridgeApiError('invalid_file', 400, 'The file could not be read.'));
    reader.readAsText(file);
  });
}

function saveAsFile(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Download the active preset as a file, or apply a file to it. Failures go to the page's error banner. */
export function usePresetFiles() {
  const { t } = useTranslation();

  const download = async () => {
    try {
      const { filename, text } = await bridgeTransport.downloadPresetFile();
      saveAsFile(filename, text);
    } catch (err) {
      bridgeTransport.reportError(err, 'file', true);
    }
  };

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = ''; // so that choosing the same file again still counts as a change
    if (!file) return;
    try {
      if (file.size > MAX_FILE_BYTES) throw new BridgeApiError('file_too_large', 413, 'The file is too large.');
      const text = await readText(file);
      try {
        JSON.parse(text);
      } catch {
        throw new BridgeApiError('invalid_file', 400, 'The file is not valid JSON.');
      }
      const ok = window.confirm(
        t(
          'preset.file.uploadConfirm',
          'Replace the content of the current preset with this file? It is applied live; press save to keep it.',
        ),
      );
      if (!ok) return;
      await bridgeTransport.uploadPresetFile(text);
    } catch (err) {
      bridgeTransport.reportError(err, 'file', true);
    }
  };

  return { download, upload };
}
