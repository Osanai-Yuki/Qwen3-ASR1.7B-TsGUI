import { useRef, useState } from "react";
import {
  AUDIO_EXTS,
  VIDEO_EXTS,
  isVideoFile,
  validateFile,
} from "../utils/fileTypes";

const ACCEPT = [...AUDIO_EXTS, ...VIDEO_EXTS, "audio/*", "video/*"].join(",");

interface Props {
  onTranscribe: (file: File, align: boolean) => void;
  disabled: boolean;
  alignerAvailable: boolean;
  /** Tighter layout for use inside a modal. */
  compact?: boolean;
}

/** Drag-and-drop / click-to-select audio/video upload with an optional alignment toggle. */
export function UploadZone({ onTranscribe, disabled, alignerAvailable, compact }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [align, setAlign] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [pickError, setPickError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const pick = (f: File | null) => {
    if (!f) return;
    // Reject unsupported types / oversized files up front — mirrors the
    // backend whitelist so a stray .txt/.pdf fails here instead of after the
    // upload round-trip. Same validator as the window-level drop path.
    const err = validateFile(f);
    if (err) {
      setFile(null);
      setPickError(err);
      return;
    }
    setPickError(null);
    setFile(f);
  };

  const submit = () => {
    if (file && !disabled) onTranscribe(file, align);
  };

  const padY = compact ? "py-8" : "py-10";

  return (
    <div>
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label="Drag and drop an audio or video file, or activate to choose a file"
        aria-disabled={disabled}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (!disabled && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (disabled) return;
          pick(e.dataTransfer.files[0] ?? null);
        }}
        className={`flex flex-col items-center justify-center cursor-pointer border-2 border-dashed ${padY} px-4 rounded-2xl transition-all duration-200 ${
          dragging
            ? "border-white/60 bg-white/15 scale-[1.01]"
            : "border-white/20 bg-white/5 backdrop-blur-md"
        } ${disabled ? "opacity-40 cursor-not-allowed" : "hover:border-white/40 hover:bg-white/10"}`}
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => {
            pick(e.target.files?.[0] ?? null);
            // Reset so picking the same file again still fires onChange
            // (otherwise re-selecting a rejected/identical file is a no-op).
            e.target.value = "";
          }}
        />
        {file ? (
          <div className="text-center animate-fade-in">
            <p className="font-mono text-sm font-bold break-all text-white">{file.name}</p>
            <p className="font-mono text-xs text-faint mt-1">
              {(file.size / 1048576).toFixed(1)} MB
            </p>
            {isVideoFile(file) && (
              <p className="font-mono text-2xs uppercase tracking-widest text-amber-400 mt-2">
                Video → MP3 (auto bitrate)
              </p>
            )}
          </div>
        ) : (
          <div className="text-center">
            <p className="font-sans text-sm text-muted">
              Drag &amp; drop audio or video here
            </p>
            <p className="font-mono text-xs text-muted mt-1">
              WAV · MP3 · FLAC · OGG · M4A · MP4 · MKV · MOV ...
            </p>
          </div>
        )}
      </div>

      {pickError && (
        <p className="font-mono text-xs text-red-400 mt-2 break-words">
          {pickError}
        </p>
      )}

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
          Word-level timestamps
          {!alignerAvailable && (
            <span className="text-muted"> (aligner unavailable)</span>
          )}
        </span>
      </label>

      <button
        onClick={submit}
        disabled={!file || disabled}
        className="btn-primary w-full mt-4 py-3"
      >
        Transcribe
      </button>
    </div>
  );
}
