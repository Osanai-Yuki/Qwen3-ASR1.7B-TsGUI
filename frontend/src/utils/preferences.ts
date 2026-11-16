// Versioned, failure-tolerant workspace preferences.
//
// localStorage holds *preferences only* — never transcript text.  Every read
// and write is guarded: a disabled or full storage must degrade to an
// in-memory copy rather than stop the app from starting.

const KEY = "qwen-asr-workspace-prefs:v1";

export interface WorkspacePreferences {
  /** Result library layout: open two-column grid or compact list. */
  listStyle: "grid" | "compact";
  /** Reader transcript presentation: grouped paragraphs or subtitle lines. */
  readerView: "paragraph" | "subtitle";
  /** Whether the import dialog asks for word-level timestamps. */
  alignmentRequested: boolean;
  /** Whether the reader defaults to "only matching segments". Off by default. */
  readerFilterMode: boolean;
}

export const DEFAULT_PREFERENCES: WorkspacePreferences = {
  listStyle: "grid",
  readerView: "paragraph",
  alignmentRequested: false,
  readerFilterMode: false,
};

let memoryFallback: WorkspacePreferences = { ...DEFAULT_PREFERENCES };
let usingMemoryFallback = false;

function storage(): Storage | null {
  try {
    if (typeof window === "undefined" || !window.localStorage) return null;
    // Probe: Safari private mode throws on setItem, and a disabled
    // localStorage can throw on access itself.
    window.localStorage.setItem("__asr_probe", "1");
    window.localStorage.removeItem("__asr_probe");
    return window.localStorage;
  } catch {
    return null;
  }
}

export function loadPreferences(): WorkspacePreferences {
  const store = storage();
  if (!store) {
    usingMemoryFallback = true;
    return { ...memoryFallback };
  }
  try {
    const raw = store.getItem(KEY);
    if (!raw) return { ...memoryFallback };
    const parsed = JSON.parse(raw) as Partial<WorkspacePreferences>;
    const merged: WorkspacePreferences = { ...DEFAULT_PREFERENCES, ...memoryFallback };
    if (parsed.listStyle === "grid" || parsed.listStyle === "compact") merged.listStyle = parsed.listStyle;
    if (parsed.readerView === "paragraph" || parsed.readerView === "subtitle") merged.readerView = parsed.readerView;
    if (typeof parsed.alignmentRequested === "boolean") merged.alignmentRequested = parsed.alignmentRequested;
    if (typeof parsed.readerFilterMode === "boolean") merged.readerFilterMode = parsed.readerFilterMode;
    memoryFallback = { ...merged };
    return merged;
  } catch {
    usingMemoryFallback = true;
    return { ...memoryFallback };
  }
}

export function savePreferences(patch: Partial<WorkspacePreferences>): WorkspacePreferences {
  const next: WorkspacePreferences = { ...loadPreferences(), ...patch };
  const store = storage();
  if (!store) {
    usingMemoryFallback = true;
    memoryFallback = next;
    return next;
  }
  try {
    store.setItem(KEY, JSON.stringify(next));
    memoryFallback = next;
    return next;
  } catch {
    usingMemoryFallback = true;
    memoryFallback = next;
    return next;
  }
}

/** True when preferences could not be persisted and live only in memory. */
export function preferencesAreDurable(): boolean {
  return !usingMemoryFallback;
}
