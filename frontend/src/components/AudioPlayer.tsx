import { useEffect, useRef } from "react";

interface Props {
  /** Object URL of the uploaded audio file (null for history records). */
  audioUrl: string | null;
  /** Called on every timeupdate with the current playback time in seconds. */
  onTimeUpdate: (time: number) => void;
  /** Seek to a specific time (triggered by clicking a transcript line). */
  seekTo: number | null;
}

/** Bottom audio player bar wired to the transcript highlight system. */
export function AudioPlayer({ audioUrl, onTimeUpdate, seekTo }: Props) {
  const audioRef = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const handler = () => onTimeUpdate(el.currentTime);
    el.addEventListener("timeupdate", handler);
    return () => el.removeEventListener("timeupdate", handler);
  }, [onTimeUpdate]);

  useEffect(() => {
    if (seekTo !== null && audioRef.current) {
      audioRef.current.currentTime = seekTo;
      audioRef.current.play().catch(() => {});
    }
  }, [seekTo]);

  if (!audioUrl) return null;

  return (
    <div className="px-4 sm:px-6 py-2.5 flex justify-center">
      <audio
        ref={audioRef}
        src={audioUrl}
        controls
        className="w-[40%] min-w-[260px] max-w-[480px] h-9 [&::-webkit-media-controls-panel]:bg-neutral-900"
      />
    </div>
  );
}
