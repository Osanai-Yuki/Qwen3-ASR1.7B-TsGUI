// Result-library querying: paged cache, race safety, invalidation.
//
// The previous implementation concatenated every response onto one array, so
// returning from the reader re-ran the effect for offset 0 and produced 108
// rows containing 72 unique results.  Here each offset is stored as its own
// page and a repeat request *replaces* that page; flattening dedupes by stable
// result id as a second line of defence.
//
// Loading is driven by two distinct triggers, kept separate on purpose:
//   * the query identity (q/filter/batch) — a new query starts from offset 0;
//   * the invalidation token — refreshes the *already-loaded* range in place.
// Merely becoming enabled again (leaving the reader) must do neither, or the
// 72 rows the user had loaded collapse back to the first page.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchHistorySearch } from "../api";
import type { HistoryItem } from "../types";

export type ResultFilter = "all" | "unseen" | "partial";

export interface HistoryQueryKey {
  q: string;
  filter: ResultFilter;
  batchId: string | null;
}

export interface HistoryResultsState {
  /** Flattened, id-deduped items in display order. */
  items: HistoryItem[];
  total: number;
  hasMore: boolean;
  loading: boolean;
  error: string;
  loadedCount: number;
  loadMore: () => void;
  reload: () => void;
  replace: (items: HistoryItem[], total: number, hasMore: boolean) => void;
}

function queryToken(key: HistoryQueryKey): string {
  return `${key.q.trim().toLocaleLowerCase()} ${key.filter} ${key.batchId ?? ""}`;
}

export function useHistoryResults(
  key: HistoryQueryKey,
  options: { enabled: boolean; pageSize: number; invalidateToken: string | number },
): HistoryResultsState {
  const { enabled, pageSize, invalidateToken } = options;
  const [pages, setPages] = useState<Record<number, HistoryItem[]>>({});
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const generationRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const pagesRef = useRef<Record<number, HistoryItem[]>>({});
  const keyRef = useRef(key);
  keyRef.current = key;
  /** Query identity already fetched since the last reset. */
  const loadedTokenRef = useRef<string | null>(null);
  /** Last invalidation token actually applied. */
  const appliedInvalidateRef = useRef<string | number>(invalidateToken);

  const token = queryToken(key);

  /** Fetch the given offsets, replacing each page. Never appends. */
  const fetchOffsets = useCallback(
    async (offsets: number[], controller: AbortController, generation: number) => {
      const current = keyRef.current;
      const collected: Record<number, HistoryItem[]> = {};
      let lastTotal = 0;
      let lastHasMore = false;
      for (const offset of offsets) {
        const response = await fetchHistorySearch({
          query: current.q,
          offset,
          limit: pageSize,
          filter: current.filter,
          batchId: current.batchId,
        });
        if (controller.signal.aborted || generation !== generationRef.current) return null;
        collected[offset] = response.items;
        lastTotal = response.total;
        lastHasMore = response.has_more;
      }
      return { collected, lastTotal, lastHasMore };
    },
    [pageSize],
  );

  /** Refresh the already-loaded range (or just page 0 when nothing is loaded). */
  const refreshLoaded = useCallback(async () => {
    if (!enabled) return;
    const generation = generationRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setError("");
    const offsets = Object.keys(pagesRef.current).map(Number).sort((a, b) => a - b);
    try {
      const result = await fetchOffsets(offsets.length ? offsets : [0], controller, generation);
      if (result === null) return;
      pagesRef.current = result.collected;
      setPages(result.collected);
      setTotal(result.lastTotal);
      setHasMore(result.lastHasMore);
    } catch (err) {
      if (controller.signal.aborted || generation !== generationRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (generation === generationRef.current && controllerRef.current === controller) {
        setLoading(false);
      }
    }
  }, [enabled, fetchOffsets]);

  // One effect covers all three triggers, kept separate from `enabled` on
  // purpose: leaving the reader flips `enabled` back on, and that must NOT
  // refetch — otherwise the 72 loaded rows collapse back to the first page.
  useEffect(() => {
    if (!enabled) return;
    const tokenChanged = loadedTokenRef.current !== token;
    const invalidated = appliedInvalidateRef.current !== invalidateToken;
    if (!tokenChanged && !invalidated) return;
    if (tokenChanged) {
      // A new query identity starts over; an invalidation keeps the loaded range.
      generationRef.current += 1;
      controllerRef.current?.abort();
      controllerRef.current = null;
      pagesRef.current = {};
      setPages({});
      setTotal(0);
      setHasMore(false);
      setError("");
    }
    loadedTokenRef.current = token;
    appliedInvalidateRef.current = invalidateToken;
    void refreshLoaded();
  }, [enabled, invalidateToken, refreshLoaded, token]);

  const loadMore = useCallback(() => {
    if (loading || !hasMore) return;
    const generation = generationRef.current;
    const nextOffset = Object.keys(pagesRef.current).reduce(
      (max, offset) => Math.max(max, Number(offset) + pageSize),
      0,
    );
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    void (async () => {
      try {
        const result = await fetchOffsets([nextOffset], controller, generation);
        if (result === null) return;
        // Replace the page at this offset rather than appending a second copy.
        const merged = { ...pagesRef.current, [nextOffset]: result.collected[nextOffset] };
        pagesRef.current = merged;
        setPages(merged);
        setTotal(result.lastTotal);
        setHasMore(result.lastHasMore);
      } catch (err) {
        if (controller.signal.aborted || generation !== generationRef.current) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (generation === generationRef.current && controllerRef.current === controller) {
          setLoading(false);
        }
      }
    })();
  }, [fetchOffsets, hasMore, loading, pageSize]);

  const reload = useCallback(() => {
    void refreshLoaded();
  }, [refreshLoaded]);

  const replace = useCallback((items: HistoryItem[], nextTotal: number, nextHasMore: boolean) => {
    pagesRef.current = { 0: items };
    setPages({ 0: items });
    setTotal(nextTotal);
    setHasMore(nextHasMore);
  }, []);

  const items = useMemo(() => {
    const offsets = Object.keys(pages).map(Number).sort((a, b) => a - b);
    const seen = new Set<string>();
    const flat: HistoryItem[] = [];
    for (const offset of offsets) {
      for (const item of pages[offset] ?? []) {
        if (seen.has(item.id)) continue;
        seen.add(item.id);
        flat.push(item);
      }
    }
    return flat;
  }, [pages]);

  return {
    items,
    total,
    hasMore,
    loading,
    error,
    loadedCount: items.length,
    loadMore,
    reload,
    replace,
  };
}
