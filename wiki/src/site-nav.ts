// Shared primary navigation for the reader and the public information pages.
export function siteNav(current = "", separator = "\n"): string {
  return [["/articles", "Articles"], ["/brain", "Brain"], ["/contribute", "Contribute"], ["/about", "About"]]
    .map(([href, label]) => `<a class="wl-navlink" href="${href}"${current === href ? ' aria-current="page"' : ""}>${label}</a>`)
    .join(separator);
}

export const SITE_TOOLS = `<a href="/recent-changes">Recent changes</a> · <a href="/stats">Stats</a> · <a href="/concepts">Concept data</a> · <a href="/mcp">Agent API</a>`;
