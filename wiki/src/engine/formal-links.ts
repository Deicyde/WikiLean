/** Navigation is narrower than portable URI storage: unfamiliar schemes remain text. */
import { endReference, type LinkEnd, type NativeItem } from "../native-companion.js";
export interface FormalLink { target: string; label: string; url?: string }
function endLabel(end: LinkEnd, target: string): string {
  const declaration = end.type === "lean" ? end.location?.declaration : undefined;
  if (typeof declaration === "string" && declaration) return declaration;
  const fragment = target.includes("#") ? target.slice(target.indexOf("#") + 1) : "";
  if (!fragment) return target;
  try { return decodeURIComponent(fragment); } catch { return fragment; }
}
/** The formal ends an item links to, in order. A checked absence (noMatch) names no formal result. */
export function formalLinks(item: NativeItem): FormalLink[] {
  return item.links.flatMap(link => link.annotation.relation === "noMatch" ? [] : link.annotation.object.map(end => {
    const target = endReference(end);
    return { target, label: endLabel(end, target), url: safeFormalUrl(target, link.base_uri) };
  }));
}
export function safeFormalUrl(target: string, base?: string): string | undefined {
  try {
    const url = base ? new URL(target, base) : new URL(target);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return undefined;
    return url.href;
  } catch { return undefined; }
}
