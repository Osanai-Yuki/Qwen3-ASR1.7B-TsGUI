/** Shared audio/video file-type whitelist, mirroring the backend whitelist so
 * rejected files fail in the browser instead of after the upload round-trip. */

export const AUDIO_EXTS = new Set([".wav", ".mp3", ".flac", ".ogg", ".m4a"]);
export const VIDEO_EXTS = new Set([
  ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv",
  ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".3gp", ".vob", ".ogv",
]);

/** Mirrors the backend ASR_MAX_UPLOAD_BYTES cap (2 GB default). */
export const MAX_UPLOAD_MB = 2048;

export function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot).toLowerCase();
}

export function isSupported(name: string): boolean {
  const e = extOf(name);
  return AUDIO_EXTS.has(e) || VIDEO_EXTS.has(e);
}

export function isVideoFile(file: File): boolean {
  if (file.type.startsWith("video/")) return true;
  return VIDEO_EXTS.has(extOf(file.name));
}

/** Client-side pre-flight check mirroring the backend whitelist and size cap.
 * Returns an error message, or null when the file is acceptable. Used by the
 * UploadZone picker and the window-level drop handler so an unsupported file
 * fails in the browser instead of after the upload round-trip. */
export function validateFile(f: File): string | null {
  if (
    !isSupported(f.name) &&
    !f.type.startsWith("audio/") &&
    !f.type.startsWith("video/")
  ) {
    return `Unsupported file type: ${f.name} — use WAV, MP3, FLAC, OGG, M4A, or a common video format.`;
  }
  if (f.size > MAX_UPLOAD_MB * 1024 * 1024) {
    return `${f.name} exceeds the ${MAX_UPLOAD_MB} MB limit.`;
  }
  return null;
}
