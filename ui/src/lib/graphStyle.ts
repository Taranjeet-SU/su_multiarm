// One mapping from node type to colour and shape, shared by the canvas and the legend, so a type is
// told apart by shape as well as colour. Types are sorted for a stable assignment.
export const SHAPES = ["ellipse", "round-rectangle", "diamond", "triangle"] as const;
export type Shape = (typeof SHAPES)[number];

export interface TypeStyle {
  series: number; // 1-based index into the theme's --series-N colours
  shape: Shape;
}

export function typeStyles(types: string[]): Map<string, TypeStyle> {
  const map = new Map<string, TypeStyle>();
  const ordered = [...new Set(types)].sort((a, b) => (a === "document" ? 1 : b === "document" ? -1 : a.localeCompare(b)));
  ordered.forEach((type, i) => map.set(type, { series: (i % 6) + 1, shape: SHAPES[Math.floor(i / 1) % SHAPES.length] }));
  return map;
}
