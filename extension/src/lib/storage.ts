import { DEFAULT_BACKEND_URL, DEFAULT_MARKET_ORIGIN, type ExtensionSettings, type InstalledWorkflow } from "./types.ts";

const SETTINGS = "bp.settings";
const WORKFLOWS = "bp.workflows";
const INPUTS = "bp.lastInputs";

export async function loadSettings(): Promise<ExtensionSettings> {
  const raw = await chrome.storage.local.get(SETTINGS);
  const s = (raw[SETTINGS] || {}) as Partial<ExtensionSettings>;
  return {
    marketToken: String(s.marketToken || ""),
    marketOrigin: String(s.marketOrigin || DEFAULT_MARKET_ORIGIN).replace(/\/+$/, ""),
    backendUrl: String(s.backendUrl || DEFAULT_BACKEND_URL).replace(/\/+$/, ""),
  };
}

export async function saveSettings(next: ExtensionSettings): Promise<void> {
  await chrome.storage.local.set({
    [SETTINGS]: {
      marketToken: next.marketToken.trim(),
      marketOrigin: (next.marketOrigin || DEFAULT_MARKET_ORIGIN).replace(/\/+$/, ""),
      backendUrl: (next.backendUrl || DEFAULT_BACKEND_URL).replace(/\/+$/, ""),
    },
  });
}

export async function listInstalled(): Promise<InstalledWorkflow[]> {
  const raw = await chrome.storage.local.get(WORKFLOWS);
  const map = (raw[WORKFLOWS] || {}) as Record<string, InstalledWorkflow>;
  return Object.values(map).sort((a, b) => b.installedAt.localeCompare(a.installedAt));
}

export async function getInstalled(id: string): Promise<InstalledWorkflow | null> {
  const raw = await chrome.storage.local.get(WORKFLOWS);
  const map = (raw[WORKFLOWS] || {}) as Record<string, InstalledWorkflow>;
  return map[id] || null;
}

export async function putInstalled(item: InstalledWorkflow): Promise<void> {
  const raw = await chrome.storage.local.get(WORKFLOWS);
  const map = (raw[WORKFLOWS] || {}) as Record<string, InstalledWorkflow>;
  map[item.id] = item;
  await chrome.storage.local.set({ [WORKFLOWS]: map });
}

export async function removeInstalled(id: string): Promise<void> {
  const raw = await chrome.storage.local.get(WORKFLOWS);
  const map = (raw[WORKFLOWS] || {}) as Record<string, InstalledWorkflow>;
  delete map[id];
  await chrome.storage.local.set({ [WORKFLOWS]: map });
}

export async function loadLastInputs(workflowId: string): Promise<Record<string, string>> {
  const raw = await chrome.storage.local.get(INPUTS);
  const map = (raw[INPUTS] || {}) as Record<string, Record<string, string>>;
  return { ...(map[workflowId] || {}) };
}

export async function saveLastInputs(workflowId: string, values: Record<string, string>): Promise<void> {
  const raw = await chrome.storage.local.get(INPUTS);
  const map = (raw[INPUTS] || {}) as Record<string, Record<string, string>>;
  const clean: Record<string, string> = {};
  for (const [k, v] of Object.entries(values)) {
    if (k && String(v).trim()) clean[k] = String(v);
  }
  if (Object.keys(clean).length) map[workflowId] = clean;
  else delete map[workflowId];
  await chrome.storage.local.set({ [INPUTS]: map });
}
