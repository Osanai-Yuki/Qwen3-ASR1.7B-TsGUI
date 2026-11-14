// Export path allocation + ZIP encoding.
//
// The final in-archive path is decided *here*, before anything is serialised,
// and the ZIP encoder is forbidden from renaming.  Previously the manifest was
// written from the pre-rename paths, so a package containing `a.txt` and
// `a_2.txt` listed both entries as `a.txt` — the archive was valid but the
// manifest did not describe it.

import type { PersistenceState } from "../types";

export const MANIFEST_NAME = "export-manifest.json";

export interface ExportCandidate {
  id: string;
  title: string;
  filename: string;
  integrity?: string;
  sourcePath?: string | null;
  persistenceState?: PersistenceState;
}

export interface LoadedExport extends ExportCandidate {
  /** Pre-normalisation relative path derived from the source/filename. */
  desiredPath: string;
  content: string;
  /** Allocation identity.  Defaults to `id`; a result exported in several
   *  formats needs one identity per produced file. */
  key?: string;
  /** Format serialised into `content`, recorded per file in the manifest. */
  format?: string;
}

export interface PlannedExport {
  id: string;
  title: string;
  finalPath: string;
  desiredPath: string;
  content: string;
  integrity: string;
  persistenceState: PersistenceState;
  warnings: string[];
  format?: string;
}

export interface ExportPlan {
  files: PlannedExport[];
  manifestPath: string;
  warnings: string[];
}

const ILLEGAL = /[<>:"/\\|?*]/g;
const SUFFIX_ID_LEN = 8;

/** A recognisable, filesystem-safe name for a bundle of exported results. */
export function exportArchiveName(items: ExportCandidate[], formats: string[]): string {
  const first = items[0];
  const leaf = stripToRelative(first?.filename || first?.title || "transcript").pop() || "transcript";
  const stem = (leaf.replace(/\.[^.]+$/, "") || "transcript").slice(0, 80).replace(/[. ]+$/, "") || "transcript";
  const count = items.length > 1 ? `_等${items.length}份` : "";
  const labels = formats.map((format) => format.replace(/[^a-z0-9]/gi, "").toUpperCase()).filter(Boolean).join("-");
  return `${stem}${count}${labels ? "_" + labels : ""}.zip`;
}

/** Drop C0 control characters without relying on regex escape sequences. */
function stripControl(value: string): string {
  let out = "";
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 0;
    if (code < 0x20 || code === 0x7f) continue;
    out += ch;
  }
  return out;
}

function stripToRelative(raw: string): string[] {
  // Normalise to forward slashes, Unicode NFC, and drop anything that could
  // escape the archive: drive letters, "." / "..", control chars, empty parts.
  const normalised = stripControl(raw.replace(/\\/g, "/").normalize("NFC"));
  const parts: string[] = [];
  for (const part of normalised.split("/")) {
    if (/^[a-zA-Z]:$/.test(part)) continue; // absolute drive letter
    const cleaned = part.normalize("NFC").replace(ILLEGAL, "_").replace(/^\.+/, "").trim();
    if (!cleaned || cleaned === "." || cleaned === "..") continue;
    parts.push(cleaned.slice(0, 120));
  }
  return parts;
}

/** Normalise one candidate to a safe relative path (no conflict handling). */
export function normalizeExportPath(
  sourcePath: string | null | undefined,
  filename: string,
  format: string,
): string {
  const parts = stripToRelative(sourcePath || filename || "transcript");
  const leaf = parts.pop() || "transcript";
  const stem = leaf.replace(/\.[^.]+$/, "") || "transcript";
  const suffix = format.replace(/[^a-z0-9]/gi, "").toLowerCase() || "txt";
  const dir = parts.filter((p) => !/^[a-zA-Z]:$/.test(p));
  return [...dir, `${stem}.${suffix}`].join("/");
}

function suffixId(id: string): string {
  const clean = id.replace(/[^0-9a-zA-Z]/g, "");
  return clean.slice(0, SUFFIX_ID_LEN) || "00000000";
}

function withSuffix(path: string, id: string, counter = 1): string {
  const at = path.lastIndexOf(".");
  // The counter must survive into the final name.  Deriving the whole tag from
  // the 8-char id alone made every retry of a collision identical — an
  // infinite loop when three results shared that id prefix.
  const tag = counter > 1 ? `__${suffixId(id)}-${counter}` : `__${suffixId(id)}`;
  return at > 0 ? `${path.slice(0, at)}${tag}${path.slice(at)}` : `${path}${tag}`;
}

/**
 * Allocate a unique final path per export.
 *
 * Collision suffixes are derived from the result's stable identity (history id
 * or task/attempt id), not from iteration order, so changing the selection
 * order cannot change any one result's final path.  Paths are compared
 * case-insensitively because the archive may be extracted on a
 * case-insensitive filesystem.
 */
export function allocateExportPaths(items: LoadedExport[]): Map<string, string> {
  const identityOf = (item: LoadedExport) => item.key || item.id;
  // Deterministic order: by identity, so the allocation is order-independent.
  const ordered = [...items].sort((a, b) => {
    const ai = identityOf(a);
    const bi = identityOf(b);
    if (ai !== bi) return ai < bi ? -1 : 1;
    return a.desiredPath < b.desiredPath ? -1 : a.desiredPath > b.desiredPath ? 1 : 0;
  });
  const taken = new Set<string>();
  const allocated = new Map<string, string>();
  for (const item of ordered) {
    const identity = identityOf(item);
    let path = item.desiredPath || "transcript.txt";
    if (taken.has(path.toLowerCase())) {
      let candidate = withSuffix(item.desiredPath || "transcript.txt", item.id);
      let n = 2;
      while (taken.has(candidate.toLowerCase())) {
        candidate = withSuffix(item.desiredPath || "transcript.txt", item.id, n);
        n += 1;
      }
      path = candidate;
    }
    taken.add(path.toLowerCase());
    allocated.set(identity, path);
  }
  return allocated;
}

/**
 * Build the whole export plan: final paths, manifest path, warnings.
 * Pure — the caller serialises from the returned plan and never re-derives a
 * path afterwards.
 */
export function buildExportPlan(items: LoadedExport[]): ExportPlan {
  const allocated = allocateExportPaths(items);
  // Reserve the manifest name first so a real result that would collide with
  // it gets the same stable suffix treatment instead of clobbering it.
  const taken = new Set<string>([MANIFEST_NAME.toLowerCase()]);
  const files: PlannedExport[] = [];
  const warnings: string[] = [];
  for (const item of items) {
    let finalPath = allocated.get(item.key || item.id) || item.desiredPath || "transcript.txt";
    if (taken.has(finalPath.toLowerCase())) {
      const base = finalPath;
      let candidate = withSuffix(base, item.id);
      let n = 2;
      while (taken.has(candidate.toLowerCase())) {
        candidate = withSuffix(base, item.id, n);
        n += 1;
      }
      finalPath = candidate;
      warnings.push(`“${item.title}”的最终文件名因冲突调整为 ${finalPath}`);
    }
    taken.add(finalPath.toLowerCase());
    const itemWarnings: string[] = [];
    if (item.desiredPath !== finalPath) {
      itemWarnings.push(`文件名冲突，已使用 ${finalPath}`);
    }
    if (item.persistenceState === "failed") {
      itemWarnings.push("这份结果尚未保存到长期记录");
    }
    files.push({
      id: item.id,
      title: item.title,
      finalPath,
      desiredPath: item.desiredPath,
      content: item.content,
      integrity: item.integrity || "complete",
      persistenceState: item.persistenceState || "saved",
      warnings: itemWarnings,
      format: item.format,
    });
  }
  let manifestPath = MANIFEST_NAME;
  if (files.some((f) => f.finalPath.toLowerCase() === manifestPath.toLowerCase())) {
    manifestPath = withSuffix(MANIFEST_NAME, "manifest");
  }
  return { files, manifestPath, warnings };
}

/** CRC32 for the ZIP entries (IEEE 802.3, reflected). */
export function crc32(bytes: Uint8Array): number {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit += 1) value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
  }
  return (value ^ 0xffffffff) >>> 0;
}

/**
 * Encode a ZIP from already-final, unique names.
 *
 * It deliberately does NOT deduplicate or rename: silently renaming here is
 * exactly how the manifest/archive mismatch happened.  A duplicate name is a
 * programmer error and throws.
 */
export function zipBlob(files: { name: string; content: string }[]): Blob {
  const encoder = new TextEncoder();
  const local: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  const seen = new Set<string>();
  let offset = 0;
  for (const file of files) {
    const name = file.name.replace(/\\/g, "/");
    const key = name.toLowerCase();
    if (seen.has(key)) {
      throw new Error(`duplicate archive path: ${name}`);
    }
    seen.add(key);
    const nameBytes = encoder.encode(name);
    const data = encoder.encode(file.content);
    const crc = crc32(data);
    const localHeader = new Uint8Array(30 + nameBytes.length);
    const ld = new DataView(localHeader.buffer);
    ld.setUint32(0, 0x04034b50, true); ld.setUint16(4, 20, true); ld.setUint16(6, 0x0800, true);
    ld.setUint16(8, 0, true); ld.setUint16(10, 0, true); ld.setUint16(12, 0x21, true);
    ld.setUint32(14, crc, true); ld.setUint32(18, data.length, true); ld.setUint32(22, data.length, true);
    ld.setUint16(26, nameBytes.length, true); ld.setUint16(28, 0, true); localHeader.set(nameBytes, 30);
    local.push(localHeader, data);
    const centralHeader = new Uint8Array(46 + nameBytes.length);
    const cd = new DataView(centralHeader.buffer);
    cd.setUint32(0, 0x02014b50, true); cd.setUint16(4, 20, true); cd.setUint16(6, 20, true);
    cd.setUint16(8, 0x0800, true); cd.setUint16(10, 0, true); cd.setUint16(12, 0, true); cd.setUint16(14, 0x21, true);
    cd.setUint32(16, crc, true); cd.setUint32(20, data.length, true); cd.setUint32(24, data.length, true);
    cd.setUint16(28, nameBytes.length, true); cd.setUint16(30, 0, true); cd.setUint16(32, 0, true);
    cd.setUint16(34, 0, true); cd.setUint16(36, 0, true); cd.setUint32(38, 0, true); cd.setUint32(42, offset, true);
    centralHeader.set(nameBytes, 46); central.push(centralHeader);
    offset += localHeader.length + data.length;
  }
  const centralSize = central.reduce((sum, part) => sum + part.length, 0);
  const end = new Uint8Array(22);
  const ed = new DataView(end.buffer);
  ed.setUint32(0, 0x06054b50, true); ed.setUint16(4, 0, true); ed.setUint16(6, 0, true);
  ed.setUint16(8, files.length, true); ed.setUint16(10, files.length, true); ed.setUint32(12, centralSize, true);
  ed.setUint32(16, offset, true); ed.setUint16(20, 0, true);
  const parts = [...local, ...central, end].map((bytes) => bytes.slice().buffer as ArrayBuffer);
  return new Blob(parts, { type: "application/zip" });
}
