/** Navigation is narrower than portable URI storage: unfamiliar schemes remain text. */
export function formalTargetLabel(target: string): string {
  const fragment = target.includes("#") ? target.slice(target.indexOf("#") + 1) : "";
  if (!fragment) return target;
  try { return decodeURIComponent(fragment); } catch { return fragment; }
}
export function safeFormalUrl(target: string, base?: string): string | undefined {
  try {
    const url = base ? new URL(target, base) : new URL(target);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return undefined;
    return url.href;
  } catch { return undefined; }
}
