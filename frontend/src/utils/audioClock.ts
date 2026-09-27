/** A tiny playback-clock store.
 *
 * Audio playback time changes ~4×/second. Pushing it through component props
 * re-renders the whole tree on every tick; instead the owner (App) holds one
 * AudioClock and writes to it, and only the components that actually need the
 * time (TranscriptPanel) subscribe. `set(null)` means "no audio loaded".
 */
export interface AudioClock {
  get(): number | null;
  set(time: number | null): void;
  /** Register a listener; it is called immediately with the current value and
   * returns an unsubscribe function for cleanup. */
  subscribe(listener: (time: number | null) => void): () => void;
}

export function createAudioClock(): AudioClock {
  let time: number | null = null;
  const listeners = new Set<(time: number | null) => void>();
  return {
    get: () => time,
    set: (next) => {
      time = next;
      listeners.forEach((l) => l(next));
    },
    subscribe: (listener) => {
      listeners.add(listener);
      listener(time);
      return () => listeners.delete(listener);
    },
  };
}
