import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { CSP_REPORT_ONLY, SECURITY_HEADERS, atlasCsp } from "../src/security.js";

// The formalization atlas pages over the golden fixture store
// (scripts/atlas/fixtures/store, bound in e2e/server.ts).
const WIDGET = "/atlas/1001/Widget_lemma";
// Its two theorems show decision D1: a conjunction covered by two parts, and an equivalence.
const GADGET = "/atlas/1003/Gadget_(mathematics)";

function collectPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) errors.push(`response ${response.status()}: ${response.url()}`);
  });
  return errors;
}

async function expectNoSeriousViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious).toEqual([]);
}

test.beforeEach(async ({ page }) => {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      throw new Error(`browser test attempted an external request: ${url.href}`);
    }
    await route.continue();
  });
});

for (const scheme of ["light", "dark"] as const) {
  test(`the coverage page and dashboard pass axe in ${scheme} mode`, async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto(WIDGET);
    await expect(page.locator("html")).toHaveAttribute("data-theme", scheme);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Widget lemma");
    await expectNoSeriousViolations(page);
    await page.goto(GADGET);
    await expect(page.locator(".atlas-coverage")).toHaveCount(1);
    await expectNoSeriousViolations(page);
    await page.goto("/atlas");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Formalization atlas");
    await expectNoSeriousViolations(page);
    expect(errors).toEqual([]);
  });

  // Phone width: the tables scroll sideways inside focusable regions, the page
  // itself never does, and each table row keeps its label in view.
  test(`at 375px the atlas pages pass axe and do not scroll sideways in ${scheme} mode`, async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await page.emulateMedia({ colorScheme: scheme });
    for (const path of [WIDGET, GADGET, "/atlas", "/atlas/universe/0e6efcaae05b9aee11c90b919b55052636ee2932901127ea571268dc3a8d6a25"]) {
      await page.goto(path);
      await expectNoSeriousViolations(page);
      const overflow = await page.locator("html").evaluate((root) => root.scrollWidth - root.clientWidth);
      expect(overflow, path).toBeLessThanOrEqual(0);
    }
    await page.goto("/atlas");
    const wrap = page.getByRole("region", { name: "Table: Articles" });
    await wrap.focus();
    await expect(wrap).toBeFocused();
    await wrap.evaluate((node) => { node.scrollLeft = node.scrollWidth; });
    const label = wrap.locator("tbody th").first();
    const box = await label.boundingBox();
    const frame = await wrap.boundingBox();
    expect(box && frame && box.x >= frame.x - 1).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("a survived coverage claim names its parts under 'Formalized by 2 parts together'", async ({ page }) => {
  await page.goto(`${GADGET}#p-a41f6c2e`);
  const passage = page.locator("#p-a41f6c2e");
  await expect(passage.locator(".atlas-pill")).toContainText("Formalized");
  const claim = passage.locator(".atlas-coverage");
  await expect(claim.locator(".atlas-why")).toHaveText("Formalized by 2 parts together:");
  await expect(claim.getByRole("listitem")).toHaveCount(2);
  await expect(claim.getByRole("listitem").first()).toContainText("Gadget.connected");
  await expect(claim.getByRole("listitem").first()).toBeVisible();
});

// The heading's pieces are flex items and the section's "§ " is CSS content:
// hidden separators keep the accessible name from running them together.
test("a passage heading's accessible name keeps its pieces apart", async ({ page }) => {
  await page.goto(WIDGET);
  await expect(page.locator("#p-91aa02de h3")).toHaveAccessibleName("Definition · central · § Definitions");
  await expect(page.getByRole("heading", { level: 3, name: "Theorem · Subject of the article · § Statement" })).toHaveCount(1);
  // The separators are read, not seen: the visible heading is unchanged.
  await expect(page.locator("#p-91aa02de h3 .atlas-sr")).toHaveCount(1);
  const hidden = await page.locator("#p-91aa02de h3 .atlas-sr").boundingBox();
  expect(hidden && hidden.width <= 1 && hidden.height <= 1).toBe(true);
  await page.goto(GADGET);
  await expect(page.locator("#p-7d2c9e10 h3")).toHaveAccessibleName("Definition · Subject of the article");
});

// The export quotes at most a passage's first 12 words; a capped head ends in
// an ellipsis outside the highlighted quotation, and the quote stays verbatim.
test("a capped quote ends in an ellipsis outside the quotation; a whole one does not", async ({ page }) => {
  await page.goto(WIDGET);
  const quote = page.locator("#p-5b0e44a1 blockquote");
  await expect(quote.locator("mark")).toHaveText("Moreover, the number of spanning gadgets of a widget is at most");
  const cut = quote.locator("[data-quote-cut]");
  await expect(cut).toBeVisible();
  await expect(cut.locator('[aria-hidden="true"]')).toHaveText(" …");
  const mark = await quote.locator("mark").boundingBox();
  const dots = await cut.locator('[aria-hidden="true"]').boundingBox();
  // After the highlight: to its right on its last line, or on a line below it.
  expect(mark && dots && (dots.x >= mark.x + mark.width - 1 || dots.y >= mark.y + mark.height - 1)).toBe(true);
  await expect(page.locator("#atlas-quote-note")).toHaveText("Quotes show at most the first 12 words of a passage " +
    "(the record format's quotation cap; a formula's MathML text counts toward it). " +
    "A … after the highlighted quote means the passage continues; “Read it in context on Wikipedia” shows the rest. " +
    "Hover a formula for its TeX.");
  await expect(page.locator("#p-3fa91c07 [data-quote-cut]")).toHaveCount(0);
  // Exactly 12 words, the whole passage: no ellipsis.
  await page.goto(GADGET);
  await expect(page.locator("#p-c93b0d58 blockquote mark")).toHaveText("Moreover, the gadgets on a widget correspond bijectively to all its vertices.");
  await expect(page.locator("[data-quote-cut]")).toHaveCount(0);
  await expect(page.locator("#atlas-quote-note")).toHaveCount(0);
});

// The vertical gap from the bottom of `a` to the top of `b` (negative when `b`
// starts above `a`'s bottom), so a test can say which comes first on the page.
async function gapBelow(page: Page, a: string, b: string): Promise<number> {
  const first = await page.locator(a).first().boundingBox();
  const second = await page.locator(b).first().boundingBox();
  expect(first && second).toBeTruthy();
  return second!.y - (first!.y + first!.height);
}

// The gap after each comma of the central-statements line is the space alone:
// the summary box's ".atlas-mini + .atlas-mini" margin does not reach it. (The
// e2e files compile without the DOM lib, so getComputedStyle is typed here.)
async function expectPlainCommaGaps(page: Page): Promise<void> {
  const margins = await page.locator("#atlas-central .atlas-mini").evaluateAll((spans) => {
    const style = Reflect.get(globalThis, "getComputedStyle") as (element: unknown) => { marginLeft: string };
    return spans.map((span) => style(span).marginLeft);
  });
  expect(margins.length).toBeGreaterThan(1);
  expect(margins.every((m) => m === "0px")).toBe(true);
}

test("the central statements are counted under the bar over all passages and its legend", async ({ page }) => {
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(WIDGET);
    await expect(page.locator(".atlas-strip .atlas-bar")).toHaveCount(1);
    await expect(page.locator("#atlas-central")).toHaveText(
      "Central statements: 2 of the 3 passages state the article's subject or a named result or definition; " +
      "the other one is supporting (examples, setup). Of these 2: ✓1 formalized, ✕1 not formalized.");
    // The bar's own counts (the Status chips) come first, then the line, then the Where row.
    expect(await gapBelow(page, ".atlas-strip .atlas-bar", '[aria-labelledby="atlas-h-status"]')).toBeGreaterThanOrEqual(0);
    expect(await gapBelow(page, '[aria-labelledby="atlas-h-status"]', "#atlas-central")).toBeGreaterThanOrEqual(0);
    expect(await gapBelow(page, "#atlas-central", '[aria-labelledby="atlas-h-where"]')).toBeGreaterThanOrEqual(0);
    await expectPlainCommaGaps(page);
    await page.goto("/atlas");
    await expect(page.locator(".atlas-strip .atlas-bar")).toHaveCount(1);
    await expect(page.locator("#atlas-central")).toHaveText(
      "Central statements: 4 of the 6 passages, across 2 articles, state an article's subject or a named result or definition; " +
      "the other 2 are supporting (examples, setup). Of these 4: ✓3 formalized, ✕1 not formalized.");
    expect(await gapBelow(page, ".atlas-legend", "#atlas-central")).toBeGreaterThanOrEqual(0);
    await expectPlainCommaGaps(page);
  }
});

test("the by-library table leads with the libraries that hold passages and lists the empty ones apart", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/atlas");
  const rows = page.getByRole("region", { name: "Table: By library" }).locator("tbody th");
  await expect(rows).toHaveText(["Mathlib", "FormalFrontier · WidgetTheory", "TauCeti", "Batteries"]);
  const more = page.locator("#atlas-libs-empty");
  // The table's column notes follow it; the list of empty libraries comes after them.
  const notes = page.locator('section[aria-labelledby="atlas-h-libs"] > p.atlas-muted');
  await expect(notes).toContainText("A passage counts toward every library that states it.");
  expect(await gapBelow(page, '[aria-label="Table: By library"]', 'section[aria-labelledby="atlas-h-libs"] > p.atlas-muted')).toBeGreaterThanOrEqual(0);
  // The list keeps the notes' margin rather than touching the table's last row.
  expect(await gapBelow(page, 'section[aria-labelledby="atlas-h-libs"] > p.atlas-muted', "#atlas-libs-empty")).toBeGreaterThan(0);
  await expect(more.locator("summary")).toHaveText("1 more library searched, with no locations in this build");
  await expect(more.getByRole("listitem")).toBeHidden();
  await more.locator("summary").click();
  await expect(more.getByRole("listitem")).toHaveText(["Lean core @1465a20"]);
  await expect(more.getByRole("link", { name: "@1465a20" })).toHaveAttribute("href", /\/tree\/1465a20b72ff3532c8f25b2532812415f8fbb0e1$/);
  await expectNoSeriousViolations(page);
  const overflow = await page.locator("html").evaluate((root) => root.scrollWidth - root.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("a passage permalink is not hidden under the sticky header", async ({ page }) => {
  await page.goto(`${WIDGET}#p-5b0e44a1`);
  const header = await page.locator(".wl-header").boundingBox();
  const pill = await page.locator("#p-5b0e44a1 .atlas-pill").boundingBox();
  expect(header && pill && pill.y >= header.y + header.height).toBe(true);
});

test("the strip's Subject row links the article's subject passage", async ({ page }) => {
  await page.goto(WIDGET);
  const subject = page.locator(".atlas-passage.is-subject");
  await expect(subject).toHaveCount(1);
  await expect(subject.locator(".atlas-subject")).toHaveText("Subject of the article");
  await page.getByRole("list", { name: "Subject" }).getByRole("link").click();
  await expect(page).toHaveURL(new RegExp(`${WIDGET}#p-3fa91c07$`));
  const header = await page.locator(".wl-header").boundingBox();
  const pill = await page.locator("#p-3fa91c07 .atlas-pill").boundingBox();
  expect(header && pill && pill.y >= header.y + header.height).toBe(true);
});

test("status and library chips filter the passages", async ({ page }) => {
  await page.goto(WIDGET);
  // The conjecture has no status, so it is set aside: three passages, and no undecided chip.
  const passages = page.locator(".atlas-passage");
  await expect(passages).toHaveCount(3);
  await expect(page.locator('.atlas-chip[data-status="unresolved"]')).toHaveCount(0);
  const chip = page.locator('.atlas-chip[data-filter="status:statement_only"]');
  await chip.click();
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(1);
  await expect(page.locator(".atlas-filter-text")).toHaveText("Showing 1 of 3 passages: Statement only.");
  // Status and library are two facets, combined: TauCeti's statement-only
  // passage is not in Mathlib.
  const mathlib = page.locator('.atlas-chip[data-filter="library:mathlib"]');
  await mathlib.click();
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  await expect(mathlib).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(0);
  await expect(page.locator(".atlas-filter-text")).toHaveText("Showing 0 of 3 passages: Statement only, in Mathlib.");
  // Pressing a pressed chip clears only its own facet.
  await chip.click();
  await expect(chip).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(1);
  await expect(page.locator(".atlas-filter-text")).toHaveText("Showing 1 of 3 passages: in Mathlib.");
  await page.getByRole("button", { name: "Show all passages" }).click();
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(3);
  await expect(mathlib).toHaveAttribute("aria-pressed", "false");
});

test("a signed-in reader confirms and reports a location", async ({ page }) => {
  const errors = collectPageErrors(page);
  await page.goto(`/login?returnTo=${encodeURIComponent(WIDGET)}`);
  await expect(page).toHaveURL(new RegExp(`${WIDGET}$`));
  const card = page.locator('.atlas-card[data-library="mathlib"]');
  await card.getByRole("button", { name: "Confirm" }).click();
  await expect(card.getByRole("status")).toHaveText("Thanks: you confirmed this link.");
  await expect(card.getByRole("button", { name: "Confirm" })).toHaveAttribute("aria-pressed", "true");
  await card.getByRole("button", { name: "Report" }).click();
  await card.getByLabel(/What is wrong with this link/).fill("States the finite case only.");
  await card.getByRole("button", { name: "Send report" }).click();
  await expect(card.getByRole("status")).toHaveText("Thanks: your report was recorded.");
  await expect(card.getByRole("button", { name: "Report" })).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(page.locator('.atlas-card[data-library="mathlib"]').getByRole("status")).toHaveText("You reported this link.");
  expect(errors).toEqual([]);
});

test("the dashboard filters its list as you type", async ({ page }) => {
  await page.goto("/atlas");
  await page.getByLabel("Search covered articles").fill("gadg");
  await expect(page.locator("#atlas-list tbody tr:visible")).toHaveCount(1);
  await expect(page.locator("#atlas-count")).toHaveText("1 of 2 articles match.");
  await expect(page.locator("#atlas-list tbody tr:visible .atlas-libtag")).toHaveText(["Mathlib"]);
});

test("an article the atlas covers shows the summary box", async ({ page }) => {
  await page.goto("/Widget_lemma");
  const box = page.getByRole("complementary", { name: "Formalization atlas" });
  // The passages with a status, as the article's atlas page counts them, and how many more there are.
  await expect(box).toContainText("3 passages of Wikipedia revision 2002");
  await expect(box).toContainText("1 more passage has no status yet.");
  await expect(box).not.toContainText("undecided");
  await expect(box).toContainText("This page shows revision");
  await expectNoSeriousViolations(page);
  await box.getByRole("link", { name: /See where/ }).click();
  await expect(page).toHaveURL(new RegExp(`${WIDGET}$`));
});

// The site's navigation links the atlas from every page's header (docs/ATLAS.md section 14,
// item 8). From an article page the header's link opens the dashboard, which alone marks it
// as the current page; at phone width the header wraps its links instead of widening the page.
test("the site header's Atlas link opens the atlas from an article page", async ({ page }) => {
  const errors = collectPageErrors(page);
  await page.goto("/Test_Article");
  const link = page.getByRole("banner").getByRole("link", { name: "Atlas", exact: true });
  await expect(link).toHaveAttribute("href", "/atlas");
  await expect(link).not.toHaveAttribute("aria-current", "page");
  await link.click();
  await expect(page).toHaveURL(/\/atlas$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Formalization atlas");
  await expect(page.getByRole("banner").getByRole("link", { name: "Atlas", exact: true })).toHaveAttribute("aria-current", "page");
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/Test_Article");
  await expect(page.getByRole("banner").getByRole("link", { name: "Atlas", exact: true })).toBeVisible();
  const overflow = await page.locator("html").evaluate((root) => root.scrollWidth - root.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  expect(errors).toEqual([]);
});

// Every CSP violation a page reports, as "<disposition> <directive> <blocked URI>".
async function watchViolations(page: Page): Promise<() => Promise<string[]>> {
  await page.addInitScript(() => {
    const seen: string[] = [];
    Reflect.set(globalThis, "__csp", seen);
    // The e2e files compile without the DOM lib, so the document is typed here.
    type Violation = { disposition: string; effectiveDirective: string; blockedURI: string };
    const doc = Reflect.get(globalThis, "document") as {
      addEventListener(type: "securitypolicyviolation", listener: (event: Violation) => void): void;
    };
    doc.addEventListener("securitypolicyviolation", (event) => {
      seen.push(`${event.disposition} ${event.effectiveDirective} ${event.blockedURI}`);
    });
  });
  return () => page.evaluate(() => Reflect.get(globalThis, "__csp") as string[]);
}

// The policy an atlas page served from `url`'s origin enforces, with the framing ban beside it.
const atlasPolicy = (url: string) => `${atlasCsp(new URL(url).origin)}, ${SECURITY_HEADERS["Content-Security-Policy"]}`;

// The atlas pages enforce their own policy (src/security.ts atlasCsp, beside the framing ban),
// as served. The article page with the summary box is not an atlas page: it gets the Worker's
// Report-Only policy (CSP_REPORT_ONLY) served as an enforced one, as enforcing it would. Every
// atlas flow works, and no violation fires, whether reported or enforced.
test("the atlas flows work under the atlas's enforced policy, and the article box under the Report-Only one enforced", async ({ page, context, baseURL }) => {
  const errors = collectPageErrors(page);
  await page.route("**/*", async (route) => {
    const request = route.request();
    if (request.resourceType() !== "document" || request.method() !== "GET") return route.fallback();
    const url = new URL(request.url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") return route.fallback();
    const response = await route.fetch({ maxRedirects: 0 });
    const headers = response.headers();
    expect(headers["content-security-policy-report-only"], url.pathname).toBe(CSP_REPORT_ONLY);
    if (url.pathname === "/atlas" || url.pathname.startsWith("/atlas/")) {
      expect(headers["content-security-policy"], url.pathname).toBe(atlasPolicy(url.href));
      return route.fulfill({ response });
    }
    expect(headers["content-security-policy"], url.pathname).toBe(SECURITY_HEADERS["Content-Security-Policy"]);
    await route.fulfill({
      response,
      headers: { ...headers, "content-security-policy": `${CSP_REPORT_ONLY}; frame-ancestors 'none'` },
    });
  });
  const violations = await watchViolations(page);
  await context.addCookies([{ name: "wl_dev_user", value: "u-patroller", url: baseURL! }]);

  // The coverage page: inline theme bootstrap, atlas.js, the filter chips and feedback.
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(WIDGET);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.locator('.atlas-chip[data-filter="status:statement_only"]').click();
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(1);
  await page.getByRole("button", { name: "Show all passages" }).click();
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(3);
  const card = page.locator('.atlas-card[data-library="mathlib"]');
  await card.getByRole("button", { name: "Confirm" }).click();
  await expect(card.getByRole("status")).toHaveText("Thanks: you confirmed this link.");
  // The coverage bar's style="width:…" still sizes its segments.
  const widths = await page.locator(".atlas-seg").evaluateAll((segs) =>
    segs.map((seg) => (seg as unknown as { style: { width: string } }).style.width));
  expect(widths.length).toBeGreaterThan(0);
  expect(widths.every((width) => /%$/.test(width))).toBe(true);
  expect(await violations()).toEqual([]);

  // The dashboard's live search (it fetches index shards), and the header search, an inline
  // script the policy allows by its hash (it fetches /api/articles).
  await page.goto("/atlas");
  await page.getByLabel("Search covered articles").fill("gadg");
  await expect(page.locator("#atlas-count")).toHaveText("1 of 2 articles match.");
  await page.locator("#wl-search").fill("Test");
  await expect(page.locator("#wl-search-list")).toBeVisible();
  await expect(page.locator("#wl-search-list a").first()).toContainText("Test Article");
  expect(await violations()).toEqual([]);

  await page.goto("/atlas/universe/0e6efcaae05b9aee11c90b919b55052636ee2932901127ea571268dc3a8d6a25");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  expect(await violations()).toEqual([]);

  // The article page's summary box.
  await page.goto("/Widget_lemma");
  await expect(page.getByRole("complementary", { name: "Formalization atlas" })).toContainText("3 passages");
  expect(await violations()).toEqual([]);
  expect(errors).toEqual([]);
});

// ---- the article reader ---------------------------------------------------------------------
//
// The third harness listener (e2e/server.ts) serves the golden store plus the Widget lemma's
// marked revision (test/helpers/atlas.ts atlasReaderFiles): its three passages with a status read
// in the order 3fa91c07, 91aa02de, 5b0e44a1 (three marks, the middle one link text); the fourth,
// 0c4d77b1, has no status, so it is plain text and set aside.
test.describe("the atlas article reader", () => {
  const READER = `http://127.0.0.1:${Number.parseInt(process.env.WIKILEAN_E2E_PORT ?? "4173", 10) + 2}`;
  test.use({ baseURL: READER });
  const panelPassage = (page: Page) => page.locator("section.atlr-pp:not([hidden])");

  for (const scheme of ["light", "dark"] as const) {
    test(`the reader passes axe with and without a passage selected in ${scheme} mode`, async ({ page }) => {
      const errors = collectPageErrors(page);
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto(WIDGET);
      await expect(page.locator("#atlr-article")).toContainText("In combinatorics, the widget lemma states that");
      await expect(page.locator("#atlr-article mark.atl")).toHaveCount(5);
      await expectNoSeriousViolations(page);
      await page.locator('#atlr-article mark.atl[data-p="3fa91c07"]').click();
      await expect(panelPassage(page)).toHaveCount(1);
      await expectNoSeriousViolations(page);
      expect(errors).toEqual([]);
    });
  }

  test("the notice heads the article column, and the panel still fits the window before any scroll", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(WIDGET);
    const notice = page.locator(".atlr-head > #atlas-notice");
    await expect(notice).toHaveText("Preliminary: these links have not yet had an independent audit.");
    const header = await page.locator(".wl-header").boundingBox();
    const box = await notice.boundingBox();
    expect(header && box && box.y >= header.y + header.height).toBe(true);
    // The sticky panel starts under the header and ends at the window's bottom edge.
    const panel = await page.locator("#atlr-panel").boundingBox();
    expect(panel && header && Math.abs(panel.y - (header.y + header.height)) <= 1).toBe(true);
    expect(panel && panel.y + panel.height <= 800 + 1).toBe(true);
  });

  test("selecting a highlight shows its status and its cards in the panel, Lean code first and Details closed", async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.goto(WIDGET);
    await expect(page.locator("#atlr-index")).toBeVisible();
    await expect(page.locator("#atlr-nav")).toBeHidden();
    await page.locator('#atlr-article mark.atl[data-p="3fa91c07"]').click();
    await expect(page).toHaveURL(new RegExp(`${WIDGET}#p-3fa91c07$`));
    const shown = panelPassage(page);
    await expect(shown).toHaveAttribute("id", "atlr-pp-3fa91c07");
    await expect(shown.locator(".atlas-pill")).toHaveText("✓Formalized");
    await expect(shown.locator(".atlas-why")).toContainText("Formalized");
    const card = shown.locator(".atlas-card").first();
    await expect(card.locator(".atlas-decl-line")).toContainText("Widget.exists_spanning_gadget");
    await expect(card.locator(".atlas-rel")).toHaveText("exact match");
    await expect(card.locator("details.atlas-prov")).not.toHaveAttribute("open", "");
    await expect(card.locator("details.atlas-prov .atlas-prov-list")).toBeHidden();
    // Signing in from the panel comes back to this passage.
    await expect(shown.locator("a.atlas-fb-signin").first()).toHaveAttribute("href", `/login?returnTo=${encodeURIComponent(`${WIDGET}#p-3fa91c07`)}`);
    await expect(page.locator("#atlr-index")).toBeHidden();
    await expect(page.locator("#atlr-pos")).toHaveText("1 of 3");
    await expect(page.locator("#atlr-mbar")).toBeVisible();
    await expect(page.locator('#atlr-article mark.atl[data-p="3fa91c07"]')).toHaveClass(/is-sel/);
    // The switch to the card list carries the passage.
    await expect(page.locator("#atlr-to-cards")).toHaveAttribute("href", `${WIDGET}?view=cards#p-3fa91c07`);
    // The stepper walks the article's reading order.
    await page.locator("#atlr-next").click();
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-91aa02de");
    await expect(panelPassage(page).locator(".atlas-absence")).toContainText("Searched 5 libraries");
    await expect(page.locator("#atlr-pos")).toHaveText("2 of 3");
    await page.getByRole("button", { name: "← All passages" }).click();
    await expect(page.locator("#atlr-index")).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${WIDGET}$`));
    expect(errors).toEqual([]);
  });

  test("the keyboard selects, steps and goes back, one stop per passage", async ({ page }) => {
    await page.goto(WIDGET);
    await expect(page.locator("#atlr-article mark.atl[role=button]")).toHaveCount(3);
    const stop = page.locator('#atlr-article mark.atl[data-p="5b0e44a1"][role=button]');
    await expect(stop).toHaveAccessibleName(/^Moreover, the number of spanning gadgets of a widget is at most: statement only, TauCeti /);
    await stop.focus();
    await page.keyboard.press("Enter");
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-5b0e44a1");
    await expect(page.locator("#atlr-pp-h-5b0e44a1")).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-3fa91c07");
    await page.keyboard.press("ArrowRight");
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-91aa02de");
    await page.keyboard.press("Escape");
    await expect(page.locator("#atlr-index")).toBeVisible();
    await expect(page.locator('#atlr-article mark.atl[data-p="91aa02de"][role=button]')).toBeFocused();
  });

  test("a #p- link opens the reader on its passage, and an unknown one says so", async ({ page }) => {
    await page.goto(`${WIDGET}#p-5b0e44a1`);
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-5b0e44a1");
    const mark = await page.locator("#p-5b0e44a1").boundingBox();
    const header = await page.locator(".wl-header").boundingBox();
    expect(mark && header && mark.y >= header.y + header.height).toBe(true);
    await page.goto(`${WIDGET}#p-00000000`);
    await expect(page.locator("#atlr-missing")).toHaveText("No passage #00000000 on this page; here are its passages.");
    // A passage with no status is set aside, so a link to it finds none either.
    await page.goto(`${WIDGET}#p-0c4d77b1`);
    await expect(page.locator("#atlr-missing")).toHaveText("No passage #0c4d77b1 on this page; here are its passages.");
    await expect(page.locator("#atlr-article")).toContainText("It is conjectured that every balanced widget is the image of a regular one.");
    await expect(page.locator("#atlr-index")).toBeVisible();
    // The index row of a passage selects it.
    await page.locator('a.atlr-row[data-p="91aa02de"]').click();
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-91aa02de");
    await expect(page).toHaveURL(new RegExp(`${WIDGET}#p-91aa02de$`));
  });

  test("the hover peek names the status and the lead declaration", async ({ page }) => {
    await page.goto(WIDGET);
    await page.locator('#atlr-article mark.atl[data-p="3fa91c07"]').hover();
    await expect(page.locator("#atlr-peek")).toBeVisible();
    await expect(page.locator("#atlr-peek")).toHaveText("✓ Formalized · Widget.exists_spanning_gadget +1 · exact match");
    await expect(page.locator('#atlr-article mark.atl[data-p="3fa91c07"]')).toHaveClass(/is-hover/);
    await page.mouse.move(5, 5);
    await expect(page.locator("#atlr-peek")).toBeHidden();
  });

  test("a status filter leaves the other passages as plain text and the stepper among the rest", async ({ page }) => {
    await page.goto(WIDGET);
    await page.locator('.atlr-chips .atlas-chip[data-filter="status:not_formalized"]').click();
    await expect(page.locator('.atlr-chips .atlas-chip[data-filter="status:not_formalized"]')).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("#atlr-filter-text")).toHaveText("Showing 1 of 3 passages: Not formalized.");
    await expect(page.locator("#atlr-article mark.atl.is-off")).toHaveCount(4);
    await expect(page.locator("a.atlr-row:visible")).toHaveCount(1);
    const off = page.locator('#atlr-article mark.atl[data-p="3fa91c07"]');
    // No wash once its transition has run.
    await expect.poll(() => off.evaluate((m) =>
      (Reflect.get(globalThis, "getComputedStyle") as (e: unknown) => { backgroundColor: string })(m).backgroundColor)).toBe("rgba(0, 0, 0, 0)");
    await off.click();
    await expect(page.locator("#atlr-index")).toBeVisible();
    await page.locator('#atlr-article mark.atl[data-p="91aa02de"]').click();
    await expect(page.locator("#atlr-pos")).toHaveText("1 of 1");
    await expect(page.locator("#atlr-next")).toBeDisabled();
    await page.getByRole("button", { name: "← All passages" }).click();
    await page.getByRole("button", { name: "Show all passages" }).click();
    await expect(page.locator("#atlr-article mark.atl.is-off")).toHaveCount(0);
  });

  test("at 375px the article is the page, a tap opens the bottom sheet, and nothing scrolls sideways", async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(WIDGET);
    const overflow = await page.locator("html").evaluate((root) => root.scrollWidth - root.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await expect(page.locator("#atlr-fab")).toBeVisible();
    await expect(page.locator("#atlr-panel")).toBeHidden();
    await expectNoSeriousViolations(page);
    await page.locator('#atlr-article mark.atl[data-p="91aa02de"]').click();
    await expect(page.locator("#atlr-panel")).toBeVisible();
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-91aa02de");
    const sheet = await page.locator("#atlr-panel").boundingBox();
    expect(sheet && sheet.height < 812 * 0.6 && sheet.y > 812 * 0.4).toBe(true);
    await expectNoSeriousViolations(page);
    await page.getByRole("button", { name: "Close the panel" }).click();
    await expect(page.locator("#atlr-panel")).toBeHidden();
    await page.locator("#atlr-fab").click();
    await expect(page.locator("#atlr-index")).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("jumps stop below the header, a filter stays with its article, formulas use a math font, and the phone sheet keeps the article", async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.goto(WIDGET);
    // Every in-page jump (Wikipedia's [1] and ↑ among them) stops below the sticky header.
    const header = await page.locator(".wl-header").boundingBox();
    const padding = await page.locator("html").evaluate((root) =>
      (Reflect.get(globalThis, "getComputedStyle") as (e: unknown) => { scrollPaddingTop: string })(root).scrollPaddingTop);
    expect(header && Number.parseFloat(padding) >= header.height).toBe(true);
    // The revision's formulas are drawn in a font with a MATH table, with what MathML Core leaves
    // out: \mathbb{R} is U+211D (not a plain R), and \log n has its thin space.
    expect(await page.locator("#atlr-article math").first().evaluate((m) =>
      (Reflect.get(globalThis, "getComputedStyle") as (e: unknown) => { fontFamily: string })(m).fontFamily)).toContain("STIX Two Math");
    await expect(page.locator('#atlr-article mi[mathvariant="double-struck"]')).toHaveText("\u211d");
    const apply = page.locator("#atlr-article mo").filter({ hasText: "\u2061" });
    await expect(apply).toHaveAttribute("rspace", "0.1667em");
    await expect(page.locator("#atlr-article annotation").first()).toHaveText("{\\displaystyle \\mathbb {R} }");
    // A filter is this article's: kept under its page id, never the tab's.
    await page.locator('.atlr-chips .atlas-chip[data-filter="status:not_formalized"]').click();
    const saved = await page.evaluate(() => {
      const store = Reflect.get(globalThis, "sessionStorage") as { getItem(key: string): string | null };
      return [store.getItem("atlr-filter"), JSON.parse(store.getItem("atlr-filter:1001") ?? "null")?.status];
    });
    expect(saved).toEqual([null, "not_formalized"]);
    await page.getByRole("button", { name: "Show all passages" }).click();
    // On a phone, the sheet leaves the article the screen above it: one header row, the passage's
    // text not repeated; its button reopens the index from the top.
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(WIDGET);
    const closed = await page.locator(".wl-header").boundingBox();
    await page.locator('#atlr-article mark.atl[data-p="91aa02de"]').click();
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-91aa02de");
    await expect(page.locator(".atlr-bar")).toBeHidden();
    await expect(panelPassage(page).locator(".atlr-clamp")).toBeHidden();
    await expect(panelPassage(page).locator(".atlas-absence")).toBeVisible();
    const open = await page.locator(".wl-header").boundingBox();
    expect(closed && open && open.height < closed.height - 20).toBe(true);
    await page.locator("#atlr-panel").evaluate((panel) => { panel.scrollTop = 200; });
    await page.getByRole("button", { name: "Close the panel" }).click();
    await expect(page.locator(".atlr-bar")).toBeVisible();
    await page.locator("#atlr-fab").click();
    await expect(page.locator("#atlr-index")).toBeVisible();
    expect(await page.locator("#atlr-panel").evaluate((panel) => panel.scrollTop)).toBe(0);
    expect(errors).toEqual([]);
  });

  // atlasCsp, as served: the shell's inline scripts run by their hashes and the reader's script
  // from its file, with no violation; a script the page did not ship, inline, as a handler or as
  // another same-origin file, never runs and is reported.
  test("the reader runs under its enforced policy with no violation, and a script the page did not ship never runs", async ({ page }) => {
    const errors = collectPageErrors(page);
    const violations = await watchViolations(page);
    await page.emulateMedia({ colorScheme: "dark" });
    const res = await page.goto(WIDGET);
    expect(res!.headers()["content-security-policy"]).toBe(atlasPolicy(page.url()));
    // The theme bootstrap ran, and atlas-reader.js.
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.locator('#atlr-article mark.atl[data-p="3fa91c07"]').click();
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-3fa91c07");
    // The header search ran: it lists WikiLean's articles from /api/articles.
    await page.locator("#wl-search").fill("Test");
    await expect(page.locator("#wl-search-list a").first()).toContainText("Test Article");
    expect(await violations()).toEqual([]);
    expect(errors).toEqual([]);
    await page.evaluate(() => {
      type El = { textContent: string; setAttribute(name: string, value: string): void; click(): void };
      const doc = Reflect.get(globalThis, "document") as {
        createElement(tag: string): El;
        body: { appendChild(node: El): void };
      };
      const script = doc.createElement("script");
      script.textContent = "globalThis.__ran = 'inline'";
      doc.body.appendChild(script);
      const button = doc.createElement("span");
      button.setAttribute("onclick", "globalThis.__ran = 'handler'");
      doc.body.appendChild(button);
      button.click();
    });
    await expect.poll(violations).toEqual(["enforce script-src-elem inline", "enforce script-src-attr inline"]);
    expect(await page.evaluate(() => Reflect.get(globalThis, "__ran"))).toBeUndefined();
    // Another script file of the site, same-origin as the page, is refused too: the policy names
    // the atlas's two files, not 'self'.
    await page.evaluate(() => {
      type El = { setAttribute(name: string, value: string): void };
      const doc = Reflect.get(globalThis, "document") as { createElement(tag: string): El; body: { appendChild(node: El): void } };
      const file = doc.createElement("script");
      file.setAttribute("src", "/assets/script.js");
      doc.body.appendChild(file);
    });
    const other = new URL("/assets/script.js", page.url()).href;
    await expect.poll(violations).toEqual(["enforce script-src-elem inline", "enforce script-src-attr inline",
      `enforce script-src-elem ${other}`]);
    // The only errors since are the browser's own notes of the refusals (Chromium and WebKit
    // write "Content Security Policy", Firefox "Content-Security-Policy").
    expect(errors.length).toBeGreaterThan(0);
    expect(errors.filter((error) => !/Content[- ]Security[- ]Policy/.test(error))).toEqual([]);
  });

  // The revision is paint-contained (atlas-reader.css): an absolutely positioned element of it with
  // the highest z-index covers the article's own box and nothing of the page around it.
  test("an overlay inside the revision covers neither the header, the panel, the page head nor the footer", async ({ page }) => {
    type Box = { x: number; y: number; width: number; height: number };
    const covered = (box: Box | null) => page.evaluate(([x, y]) => {
      const doc = Reflect.get(globalThis, "document") as {
        elementFromPoint(x: number, y: number): { closest(selector: string): unknown } | null;
      };
      return doc.elementFromPoint(x, y)?.closest("#e2e-overlay") != null;
    }, [box!.x + box!.width / 2, box!.y + Math.min(box!.height / 2, 20)]);
    const inject = () => page.evaluate(() => {
      const doc = Reflect.get(globalThis, "document") as {
        createElement(tag: string): { id: string; setAttribute(name: string, value: string): void };
        querySelector(selector: string): { appendChild(node: unknown): void } | null;
      };
      const overlay = doc.createElement("div");
      overlay.id = "e2e-overlay";
      overlay.setAttribute("style", "position:absolute;top:-3000px;left:-3000px;width:9000px;height:20000px;" +
        "z-index:2147483647;background:#c00");
      doc.querySelector("#atlr-article .mw-parser-output")!.appendChild(overlay);
    });
    await page.goto(WIDGET);
    await expect(page.locator("#atlr-article mark.atl")).toHaveCount(5);
    await inject();
    expect(await covered(await page.locator(".atlr-head h1").boundingBox())).toBe(false);
    expect(await covered(await page.locator(".wl-header").boundingBox())).toBe(false);
    expect(await covered(await page.locator("#atlr-panel").boundingBox())).toBe(false);
    // It is there: the article's own box is under it.
    const hatnote = page.locator("#atlr-article .hatnote");
    await hatnote.scrollIntoViewIfNeeded();
    expect(await covered(await hatnote.boundingBox())).toBe(true);
    // Clipped to the article, it does not make the page 17,000px taller either.
    expect(await page.locator("html").evaluate((root) => root.scrollHeight)).toBeLessThan(8000);
    await page.locator("footer.atlr-foot").scrollIntoViewIfNeeded();
    expect(await covered(await page.locator("footer.atlr-foot").boundingBox())).toBe(false);
    // On a phone, the header and the panel's button stay on top too.
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(WIDGET);
    await expect(page.locator("#atlr-fab")).toBeVisible();
    await inject();
    expect(await covered(await page.locator(".wl-header").boundingBox())).toBe(false);
    expect(await covered(await page.locator("#atlr-fab").boundingBox())).toBe(false);
  });

  test("the card list is one click away and leads back to each passage in the article", async ({ page }) => {
    await page.goto(WIDGET);
    await page.locator("#atlr-to-cards").click();
    await expect(page).toHaveURL(new RegExp(`${WIDGET}\\?view=cards$`));
    await expect(page.locator(".atlas-passages > .atlas-passage")).toHaveCount(3);
    await expect(page.locator('.atlr-switch a[aria-current="page"]')).toHaveText("Card list");
    await expectNoSeriousViolations(page);
    await page.locator("#p-5b0e44a1").getByRole("link", { name: "Show in article" }).click();
    await expect(page).toHaveURL(new RegExp(`${WIDGET}#p-5b0e44a1$`));
    await expect(panelPassage(page)).toHaveAttribute("id", "atlr-pp-5b0e44a1");
    // From a card-list passage, the switch keeps the passage.
    await page.goto(`${WIDGET}?view=cards#p-91aa02de`);
    await expect(page.locator("#atlr-to-article")).toHaveAttribute("href", `${WIDGET}#p-91aa02de`);
  });

  test("a signed-in reader confirms a location from the panel", async ({ page }) => {
    const errors = collectPageErrors(page);
    await page.goto(`/login?returnTo=${encodeURIComponent(WIDGET)}`);
    await expect(page).toHaveURL(new RegExp(`${WIDGET}$`));
    await page.locator('#atlr-article mark.atl[data-p="3fa91c07"]').click();
    const card = panelPassage(page).locator('.atlas-card[data-library="mathlib"]');
    await card.getByRole("button", { name: "Confirm" }).click();
    await expect(card.getByRole("status")).toHaveText("Thanks: you confirmed this link.");
    expect(errors).toEqual([]);
  });
});
