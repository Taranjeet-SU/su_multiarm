/** Splits ``text`` into plain parts and the citation ids it mentions verbatim (longest id first). */
export function citationParts(text: string, ids: string[]): Array<{ text: string; cite: string | null }> {
  const sorted = [...new Set(ids.filter((id) => id.length >= 3))].sort((a, b) => b.length - a.length);
  const parts: Array<{ text: string; cite: string | null }> = [];
  let rest = text;
  while (rest.length) {
    let best = -1;
    let id = "";
    for (const candidate of sorted) {
      const at = rest.indexOf(candidate);
      if (at >= 0 && (best < 0 || at < best)) {
        best = at;
        id = candidate;
      }
    }
    if (best < 0) {
      parts.push({ text: rest, cite: null });
      break;
    }
    if (best > 0) parts.push({ text: rest.slice(0, best), cite: null });
    parts.push({ text: id, cite: id });
    rest = rest.slice(best + id.length);
  }
  return parts;
}

