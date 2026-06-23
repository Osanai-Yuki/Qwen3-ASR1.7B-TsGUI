import { useEffect, useRef } from "react";

interface Props {
  /** Object URL of the uploaded audio file (null for history records). */
  audioUrl: string | null;
  /** Called on every timeupdate with the current playback time in seconds. */
  onTimeUpdate: (time: number) => void;
  /** Seek to a specific time (triggered by clicking a transcript line). */
  seekTo: number | null;
}

/** Minimal audio player wired to the transcript highlight system. */
export function AudioPlayer({ audioUrl, onTimeUpdate, seekTo }: Props) {
  const audioRef = useRef<HTMLAudioElement>(null);

  // Propagate timeupdate events upward
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const handler = () => onTimeUpdate(el.currentTime);
    el.addEventListener("timeupdate", handler);
    return () => el.removeEventListener("timeupdate", handler);
  }, [onTimeUpdate]);

  // Handle external seek requests (from clicking a transcript line)
  useEffect(() => {
    if (seekTo !== null && audioRef.current) {
      audioRef.current.currentTime = seekTo;
      audioRef.current.play().catch(() => {});
    }
  }, [seekTo]);

  if (!audioUrl) return null;

  return (
    <div className="border border-neutral-800 p-4 animate-fade-in">
      <audio
        ref={audioRef}
        src={audioUrl}
        controls
        className="w-full h-8 [&::-webkit-media-controls-panel]:bg-neutral-900"
      />
    </div>
  );
}
