// One place for the dashboard's saved keys and API address, shared by every page.
// Pages used to store the read key under three different names, so a key entered on one
// page was missing on the next; older names are migrated on first read.

const READ_KEY = "cw_read_key";
const ADMIN_KEY = "cw_admin_key";
const ENDPOINT = "cw_endpoint";

const LEGACY_READ_KEYS = ["cw_api_key", "changewatch_api_key"];
const LEGACY_ADMIN_KEYS = ["changewatch_admin_key"];
const LEGACY_ENDPOINTS = ["changewatch_endpoint"];
// Placeholder that an older analytics page used as a default; never a real key
const PLACEHOLDER_KEYS = new Set(["cw_read_test_key"]);

function read(name: string): string {
  try {
    return window.localStorage.getItem(name) || "";
  } catch {
    return ""; // storage blocked (private window, previews)
  }
}

function write(name: string, value: string): void {
  try {
    if (value) window.localStorage.setItem(name, value);
    else window.localStorage.removeItem(name);
  } catch {
    // storage blocked: the value just is not remembered
  }
}

function readWithLegacy(name: string, legacy: string[]): string {
  const current = read(name);
  if (current && !PLACEHOLDER_KEYS.has(current)) return current;
  for (const old of legacy) {
    const value = read(old);
    if (value && !PLACEHOLDER_KEYS.has(value)) {
      write(name, value);
      return value;
    }
  }
  return "";
}

export function loadReadKey(): string {
  return readWithLegacy(READ_KEY, LEGACY_READ_KEYS);
}

export function saveReadKey(value: string): void {
  write(READ_KEY, value.trim());
}

export function loadAdminKey(): string {
  return readWithLegacy(ADMIN_KEY, LEGACY_ADMIN_KEYS);
}

export function saveAdminKey(value: string): void {
  write(ADMIN_KEY, value.trim());
}

// Empty means "same origin": the dashboard proxies /api and /v1 to the API (next.config.js),
// which also works behind the Caddy domain where localhost:8000 is unreachable
export function loadEndpoint(): string {
  return readWithLegacy(ENDPOINT, LEGACY_ENDPOINTS);
}

export function saveEndpoint(value: string): void {
  write(ENDPOINT, value.trim());
}

export function apiUrl(endpoint: string, path: string): string {
  if (!endpoint) return path;
  return `${endpoint.trim().replace(/\/+$/, "")}${path}`;
}

// CSV exports need the X-API-Key header, which a plain link or window.open cannot send
export async function downloadWithKey(url: string, key: string, filename: string): Promise<void> {
  const res = await fetch(url, { headers: { "X-API-Key": key } });
  if (!res.ok) throw new Error(`Export failed (${res.status})`);
  const href = URL.createObjectURL(await res.blob());
  const link = document.createElement("a");
  link.href = href;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(href);
}
