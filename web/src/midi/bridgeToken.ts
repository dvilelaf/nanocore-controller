const STORAGE_KEY = 'nanocore-bridge-token';

let token: string | null = null;

function stored(): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function store(value: string | null): void {
  try {
    if (value === null) window.sessionStorage.removeItem(STORAGE_KEY);
    else window.sessionStorage.setItem(STORAGE_KEY, value);
  } catch {
    // Blocked storage only means a reload needs the full address again.
  }
}

/**
 * Reads `#token=...` from the address bar once, keeps it in module memory and removes it from
 * the URL so it does not linger in the history entry, bookmarks or screenshots. It is also kept
 * in this tab's session storage so a reload does not drop the connection; session storage is
 * private to the tab and gone when it closes, and the token is never put in local storage.
 * Calling it again returns the token already captured.
 */
export function captureTokenFromUrl(
  loc: Pick<Location, 'hash' | 'pathname' | 'search'> = window.location,
  hist: Pick<History, 'replaceState' | 'state'> = window.history,
): string | null {
  const params = new URLSearchParams(loc.hash.replace(/^#/, ''));
  const found = params.get('token');
  if (found) {
    token = found;
    store(found);
    params.delete('token');
    const rest = params.toString();
    hist.replaceState(hist.state, '', `${loc.pathname}${loc.search}${rest ? `#${rest}` : ''}`);
  } else if (token === null) {
    token = stored();
  }
  return token;
}

/**
 * When the page was served by `nanocore serve --no-token` there is no token in the address, but
 * the server says so on its health route. The empty string then means "connect without one".
 * Nothing is stored. Anything that is not such a server leaves the editor without a pedal.
 */
export async function detectTokenlessServer(fetchFn: typeof fetch = fetch): Promise<boolean> {
  if (token !== null) return false;
  try {
    const response = await fetchFn('/api/health', { cache: 'no-store' });
    if (!response.ok) return false;
    const body: unknown = await response.json();
    if (typeof body === 'object' && body !== null && (body as { token_required?: unknown }).token_required === false) {
      token = '';
      return true;
    }
  } catch {
    // Not served by nanocore, or unreachable: there is no server.
  }
  return false;
}

export function getBridgeToken(): string | null {
  return token;
}

export function hasBridgeToken(): boolean {
  return token !== null;
}

/** Test helper. */
export function resetBridgeTokenForTests(): void {
  token = null;
  store(null);
}

/** Test helper: what a page reload does to the module. */
export function forgetTokenInMemoryForTests(): void {
  token = null;
}
