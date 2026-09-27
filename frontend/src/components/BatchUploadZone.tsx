import { useMemo, useRef, useState } from "react";
import {
  AUDIO_EXTS,
  VIDEO_EXTS,
  MAX_UPLOAD_MB,
  extOf,
  isSupported,
} from "../utils/fileTypes";
import { IconFolder, IconPlus } from "./icons";

type Kind = "all" | "audio" | "video";

function fmtMB(bytes: number): string {
  return (bytes / 1048576).toFixed(1) + " MB";
}

interface Props {
  /** The current selection — owned by the parent so it survives across drops
   * (the parent no longer remounts this component on every drop). */
  picked: File[];
  /** Report a change to the selection (supports a functional updater so the
   * child can merge against the latest list without races). */
  onPickedChange: (updater: File[] | ((prev: File[]) => File[])) => void;
  /** Enqueue the selection; resolves to null on success or an error message
   * on failure (shown inline — the global error bar sits behind this modal). */
  onEnqueue: (files: File[], align: boolean) => Promise<string | null>;
  disabled: boolean;
  alignerAvailable: boolean;
}

/**
 * Folder / multi-file picker with client-side filtering for batch enqueue.
 * Filters mirror the backend whitelist so rejected files never leave the
 * browser: type kind, minimum size, filename keyword, per-file 2 GB cap.
 *
 * `picked` is controlled by the parent (App.tsx), which accumulates drops.
 * This component never owns the list, so adding folder A then folder B always
 * yields [A, B] — the previous drop is never discarded.
 */
export function BatchUploadZone({
  picked,
  onPickedChange,
  onEnqueue,
  disabled,
  alignerAvailable,
}: Props) {
  const [align, setAlign] = useState(false);
  const [kind, setKind] = useState<Kind>("all");
  const [minSizeMB, setMinSizeMB] = useState("");
  const [keyword, setKeyword] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dirInputRef = useRef<HTMLInputElement>(null);
  const filesInputRef = useRef<HTMLInputElement>(null);

  const addFiles = (list: FileList | null) => {
    if (!list || list.length === 0) return;
    setNotice(null);
    setError(null);
    // Merge into the parent-owned selection, de-duplicating by name+size
    // (folder re-picks are common). The parent list is the single source of
    // truth, so a drop always appends to what's already there.
    const incoming = Array.from(list);
    onPickedChange((prev) => {
      const seen = new Set(prev.map((f) => `${f.name}__${f.size}`));
      const next = [...prev];
      for (const f of incoming) {
        if (!seen.has(`${f.name}__${f.size}`)) next.push(f);
      }
      return next;
    });
  };

  const { kept, oversized, unsupported } = useMemo(() => {
    const minBytes = (parseFloat(minSizeMB) || 0) * 1048576;
    const kw = keyword.trim().toLowerCase();
    const kept: File[] = [];
    let oversized = 0;
    let unsupported = 0;
    for (const f of picked) {
      if (!isSupported(f.name)) {
        unsupported += 1;
        continue;
      }
      const e = extOf(f.name);
      if (kind === "audio" && !AUDIO_EXTS.has(e)) continue;
      if (kind === "video" && !VIDEO_EXTS.has(e)) continue;
      if (f.size < minBytes) continue;
      if (kw && !f.name.toLowerCase().includes(kw)) continue;
      if (f.size > MAX_UPLOAD_MB * 1024 * 1024) {
        oversized += 1;
        continue;
      }
      kept.push(f);
    }
    return { kept, oversized, unsupported };
  }, [picked, kind, minSizeMB, keyword]);

  const totalBytes = useMemo(
    () => kept.reduce((s, f) => s + f.size, 0),
    [kept],
  );

  const submit = async () => {
    if (kept.length === 0 || busy || disabled) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const err = await onEnqueue(kept, align && alignerAvailable);
      if (err === null) {
        onPickedChange([]);
        setNotice(`${kept.length} file(s) added to the queue.`);
      } else {
        setError(err);
      }
    } finally {
      setBusy(false);
    }
  };

  const filterInput =
    "bg-white/5 border border-white/15 rounded-lg px-2 py-1.5 font-mono " +
    "text-xs text-muted focus:border-white/40 w-full";

  return (
    <div>
      {/* Pickers */}
      <div className="grid grid-cols-2 gap-2">
        <button
          onClick={() => !disabled && dirInputRef.current?.click()}
          disabled={disabled || busy}
          className="btn-ghost py-6 flex flex-col items-center gap-1"
        >
          <IconFolder className="w-5 h-5" />
          <span className="text-xs">Select folder</span>
        </button>
        <button
          onClick={() => !disabled && filesInputRef.current?.click()}
          disabled={disabled || busy}
          className="btn-ghost py-6 flex flex-col items-center gap-1"
        >
          <IconPlus className="w-5 h-5" />
          <span className="text-xs">Select files</span>
        </button>
        <input
          ref={dirInputRef}
          type="file"
          multiple
          className="hidden"
          {...({ webkitdirectory: "" } as Record<string, string>)}
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <input
          ref={filesInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {/* Filters */}
      <div className="grid grid-cols-3 gap-2 mt-3">
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as Kind)}
          className={filterInput}
          aria-label="File type filter"
        >
          <option value="all">All types</option>
          <option value="audio">Audio only</option>
          <option value="video">Video only</option>
        </select>
        <input
          type="number"
          min="0"
          placeholder="Min MB"
          value={minSizeMB}
          onChange={(e) => setMinSizeMB(e.target.value)}
          className={filterInput}
          aria-label="Minimum file size in MB"
        />
        <input
          type="text"
          placeholder="Name contains…"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          className={filterInput}
          aria-label="Filename keyword filter"
        />
      </div>

      {/* Selection preview */}
      <div className="mt-3 font-mono text-xs text-muted">
        {picked.length === 0 ? (
          <p className="text-muted">
            Pick a folder or files — they are filtered locally before upload.
          </p>
        ) : (
          <p>
            <span className="text-white font-bold">{kept.length}</span>
            {" of "}{picked.length}{" selected · "}{fmtMB(totalBytes)}
            {unsupported > 0 && (
              <span className="text-muted"> · {unsupported} unsupported skipped</span>
            )}
            {oversized > 0 && (
              <span className="text-red-400"> · {oversized} over {MAX_UPLOAD_MB} MB excluded</span>
            )}
          </p>
        )}
      </div>
      {kept.length > 0 && (
        <ul className="mt-2 max-h-36 overflow-y-auto space-y-0.5">
          {kept.slice(0, 100).map((f) => (
            <li
              key={`${f.name}__${f.size}`}
              className="font-mono text-[11px] text-muted truncate flex justify-between gap-2"
            >
              <span className="truncate" title={f.name}>{f.name}</span>
              <span className="text-dim shrink-0">{fmtMB(f.size)}</span>
            </li>
          ))}
          {kept.length > 100 && (
            <li className="font-mono text-[11px] text-muted">
              … and {kept.length - 100} more
            </li>
          )}
        </ul>
      )}

      {/* Align toggle (applies to the whole batch) */}
      <label
        className={`flex items-center gap-3 mt-4 font-mono text-sm text-muted ${
          !alignerAvailable || disabled ? "opacity-40" : "cursor-pointer"
        }`}
      >
        <input
          type="checkbox"
          checked={align && alignerAvailable}
          disabled={!alignerAvailable || disabled}
          onChange={(e) => setAlign(e.target.checked)}
          className="w-4 h-4 accent-white"
        />
        <span>
          Word-level timestamps (whole batch)
          {!alignerAvailable && (
            <span className="text-muted"> (aligner unavailable)</span>
          )}
        </span>
      </label>

      {notice && (
        <p className="font-mono text-xs text-emerald-400 mt-2">{notice}</p>
      )}

      {error && (
        <p className="font-mono text-xs text-red-400 mt-2 break-words">
          {error}
        </p>
      )}

      <div className="flex gap-2 mt-4">
        <button
          onClick={submit}
          disabled={kept.length === 0 || disabled || busy}
          className="btn-primary flex-1 py-3"
        >
          {busy ? "Uploading…" : `Add to queue (${kept.length})`}
        </button>
        {picked.length > 0 && (
          <button
            onClick={() => {
              onPickedChange([]);
              setNotice(null);
            }}
            disabled={busy}
            className="btn-ghost px-4"
            title="Clear selection"
          >
            Clear
          </button>
        )}
      </div>
    </div>
  );
}
