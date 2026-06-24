import { useRef, useState } from "react";

const ACCEPT =
  ".wav,.mp3,.flac,.ogg,.m4a,audio/*," +
  ".mp4,.mkv,.mov,.avi,.webm,.flv,.m4v,.wmv,.mpg,.mpeg,.ts,.3gp,video/*";

const VIDEO_EXTS = new Set([
  ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv",
  ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".3gp",
]);

function isVideoFile(file: File): boolean {
  if (file.type.startsWith("video/")) return true;
  const dot = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  return dot !== "" && VIDEO_EXTS.has(dot);
}

interface Props {
  onTranscribe: (file: File, align: boolean) => void;
  disabled: boolean;
  alignerAvailable: boolean;
}

/** Drag-and-drop / click-to-select audio/video upload with an optional alignment toggle. */
export function UploadZone({ onTranscribe, disabled, alignerAvailable }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [align, setAlign] = useState(false);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const pick = (f: File | null) => {
    if (f) setFile(f);
  };

  const submit = () => {
    if (file && !disabled) onTranscribe(file, align);
  };

  return (
    <div className="border border-neutral-800 p-6">
      <h2 className="font-mono text-xs uppercase tracking-widest text-neutral-500 mb-4">
        Upload Audio
      </h2>

      <div
        onClick={() => !disabled && inputRef.current?.click()}
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
        className={`flex flex-col items-center justify-center cursor-pointer border-2 border-dashed py-10 px-4 transition-colors ${
          dragging ? "border-white bg-neutral-900" : "border-neutral-700"
        } ${disabled ? "opacity-40 cursor-not-allowed" : "hover:border-neutral-500"}`}
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => pick(e.target.files?.[0] ?? null)}
        />
        {file ? (
          <div className="text-center">
            <p className="font-mono text-sm font-bold break-all">{file.name}</p>
            <p className="font-mono text-xs text-neutral-500 mt-1">
              {(file.size / 1048576).toFixed(1)} MB
            </p>
            {isVideoFile(file) && (
              <p className="font-mono text-[10px] uppercase tracking-widest text-amber-600 mt-2">
                Video → MP3 (auto bitrate)
              </p>
            )}
          </div>
        ) : (
          <div className="text-center">
            <p className="font-mono text-sm text-neutral-300">
              Drag &amp; drop audio or video here
            </p>
            <p className="font-mono text-xs text-neutral-600 mt-1">
              WAV · MP3 · FLAC · OGG · M4A · MP4 · MKV · MOV ...
            </p>
          </div>
        )}
      </div>

      <label
        className={`flex items-center gap-3 mt-4 font-mono text-sm ${
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
            <span className="text-neutral-600"> (aligner unavailable)</span>
          )}
        </span>
      </label>

      <button
        onClick={submit}
        disabled={!file || disabled}
        className="w-full mt-4 py-3 font-mono text-sm font-bold uppercase tracking-widest bg-white text-black disabled:bg-neutral-800 disabled:text-neutral-600 disabled:cursor-not-allowed hover:bg-neutral-200 transition-colors"
      >
        Transcribe
      </button>
    </div>
  );
}
