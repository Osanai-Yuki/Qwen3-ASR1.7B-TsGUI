// Import-draft identity.
//
// Previously `addFiles` deduped on `path + size`, which silently dropped two
// ordinary File objects with the same name and length but different content —
// a file picker gives no directory, so the two are indistinguishable by that
// key and one vanished.  Every discovered File now gets a stable
// `clientId`, and receipts are matched by that id rather than by guessing
// from a name or size.

export interface ImportItem {
  key: string;
  file: File;
  /** Best-known source path; `file.name` when the picker gave no directory. */
  path: string;
  selected: boolean;
  reason: string | null;
  clientId: string;
  /** True when the source directory is genuinely unknown (plain file picker). */
  sourceUnknown: boolean;
}

export function newClientId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID().replace(/-/g, "").slice(0, 16);
  }
  return Array.from(crypto.getRandomValues(new Uint8Array(8)), (n) =>
    n.toString(16).padStart(2, "0"),
  ).join("");
}

function pathOf(file: File): { path: string; sourceUnknown: boolean } {
  const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
  if (relative && relative.trim()) return { path: relative, sourceUnknown: false };
  return { path: file.name, sourceUnknown: true };
}

/**
 * Append newly discovered files.  No name/size dedupe: two same-named
 * same-sized files are kept as separate items unless the user removes one.
 * Re-selecting the *same* path (a folder re-pick) is also kept separate —
 * dedupeing there would require asserting identical content, which the
 * picker cannot prove.
 */
export function addImportItems(
  previous: ImportItem[],
  incoming: File[],
  reasonFor: (file: File) => string | null,
): ImportItem[] {
  const next = [...previous];
  for (const file of incoming) {
    const { path, sourceUnknown } = pathOf(file);
    const clientId = newClientId();
    next.push({
      key: clientId,
      file,
      path,
      selected: true,
      reason: reasonFor(file),
      clientId,
      sourceUnknown,
    });
  }
  return next;
}

/**
 * Remove exactly the items whose client ids appear in the receipt.
 *
 * Matching by client id is what makes a partial accept safe: the first item
 * may be accepted and the second rejected, and only the first disappears —
 * even if the two share a name and size.
 */
export function reconcileReceipts(items: ImportItem[], acceptedClientIds: string[]): ImportItem[] {
  const accepted = new Set(acceptedClientIds);
  return items.filter((item) => !accepted.has(item.clientId));
}

/** Client ids for the items that should actually be submitted. */
export function submitIds(items: ImportItem[]): string[] {
  return items.filter((item) => item.selected && !item.reason).map((item) => item.clientId);
}
