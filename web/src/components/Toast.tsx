import { useState } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

/** A notice that floats over the page instead of taking room in it; it can be dismissed. */
export function Toast({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [hidden, setHidden] = useState(false);
  if (hidden) return null;
  return (
    <div className="app-notice" role="status">
      <span>{children}</span>
      <button type="button" className="btn btn--ghost btn--small" onClick={() => setHidden(true)}>
        {t('bridge.error.dismiss', 'Dismiss')}
      </button>
    </div>
  );
}
