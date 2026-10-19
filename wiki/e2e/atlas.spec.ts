import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { CSP_REPORT_ONLY, SECURITY_HEADERS } from "../src/security.js";

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
  await expect(page.locator("#atlas-quote-note")).toContainText("Each quote is at most the first 12 words of its passage.");
  await expect(page.locator("#p-3fa91c07 [data-quote-cut]")).toHaveCount(0);
  // Exactly 12 words, the whole passage: no ellipsis.
  await page.goto(GADGET);
  await expect(page.locator("#p-c93b0d58 blockquote mark")).toHaveText("Moreover, the gadgets on a widget correspond bijectively to all its vertices.");
  await expect(page.locator("[data-quote-cut]")).toHaveCount(0);
  await expect(page.locator("#atlas-quote-note")).toHaveCount(0);
});

test("the central statements are counted next to the bar over all passages", async ({ page }) => {
  await page.goto(WIDGET);
  await expect(page.locator(".atlas-strip .atlas-bar")).toHaveCount(1);
  await expect(page.locator("#atlas-central")).toHaveText(
    "Central statements, the article's subject and central passages: 2 of 4 passages — ✓1 formalized, ✕1 not formalized.");
  await page.goto("/atlas");
  await expect(page.locator(".atlas-strip .atlas-bar")).toHaveCount(1);
  await expect(page.locator("#atlas-central")).toHaveText(
    "Central statements, the subject and central passages of every article: 4 of 7 passages — ✓3 formalized, ✕1 not formalized.");
});

test("the by-library table leads with the libraries that hold passages and lists the empty ones apart", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/atlas");
  const rows = page.getByRole("region", { name: "Table: By library" }).locator("tbody th");
  await expect(rows).toHaveText(["Mathlib", "FormalFrontier · WidgetTheory", "TauCeti", "Batteries"]);
  const more = page.locator("#atlas-libs-empty");
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
  const passages = page.locator(".atlas-passage");
  await expect(passages).toHaveCount(4);
  const chip = page.locator('.atlas-chip[data-filter="status:statement_only"]');
  await chip.click();
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(1);
  await expect(page.locator(".atlas-filter-text")).toHaveText("Showing 1 of 4 passages: Statement only.");
  // Status and library are two facets, combined: TauCeti's statement-only
  // passage is not in Mathlib.
  const mathlib = page.locator('.atlas-chip[data-filter="library:mathlib"]');
  await mathlib.click();
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  await expect(mathlib).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(0);
  await expect(page.locator(".atlas-filter-text")).toHaveText("Showing 0 of 4 passages: Statement only, in Mathlib.");
  // Pressing a pressed chip clears only its own facet.
  await chip.click();
  await expect(chip).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(1);
  await expect(page.locator(".atlas-filter-text")).toHaveText("Showing 1 of 4 passages: in Mathlib.");
  await page.getByRole("button", { name: "Show all passages" }).click();
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(4);
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
  await expect(box).toContainText("4 passages of Wikipedia revision 2002");
  await expect(box).toContainText("This page shows revision");
  await expectNoSeriousViolations(page);
  await box.getByRole("link", { name: /See where/ }).click();
  await expect(page).toHaveURL(new RegExp(`${WIDGET}$`));
});

// The Worker's Report-Only policy (src/security.ts CSP_REPORT_ONLY), served as
// an enforced Content-Security-Policy on every document, as enforcing it
// would. Every atlas flow still works, and no violation fires, whether
// reported or enforced.
test("the atlas flows work with the Worker's Report-Only policy enforced", async ({ page, context, baseURL }) => {
  const errors = collectPageErrors(page);
  await page.route("**/*", async (route) => {
    const request = route.request();
    if (request.resourceType() !== "document" || request.method() !== "GET") return route.fallback();
    const url = new URL(request.url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") return route.fallback();
    const response = await route.fetch({ maxRedirects: 0 });
    const headers = response.headers();
    expect(headers["content-security-policy-report-only"], url.pathname).toBe(CSP_REPORT_ONLY);
    expect(headers["content-security-policy"], url.pathname).toBe(SECURITY_HEADERS["Content-Security-Policy"]);
    await route.fulfill({
      response,
      headers: { ...headers, "content-security-policy": `${CSP_REPORT_ONLY}; frame-ancestors 'none'` },
    });
  });
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
  const violations = () => page.evaluate(() => Reflect.get(globalThis, "__csp") as string[]);
  await context.addCookies([{ name: "wl_dev_user", value: "u-patroller", url: baseURL! }]);

  // The coverage page: inline theme bootstrap, atlas.js, the filter chips and feedback.
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(WIDGET);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.locator('.atlas-chip[data-filter="status:statement_only"]').click();
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(1);
  await page.getByRole("button", { name: "Show all passages" }).click();
  await expect(page.locator(".atlas-passage:visible")).toHaveCount(4);
  const card = page.locator('.atlas-card[data-library="mathlib"]');
  await card.getByRole("button", { name: "Confirm" }).click();
  await expect(card.getByRole("status")).toHaveText("Thanks: you confirmed this link.");
  // The coverage bar's style="width:…" still sizes its segments.
  const widths = await page.locator(".atlas-seg").evaluateAll((segs) =>
    segs.map((seg) => (seg as unknown as { style: { width: string } }).style.width));
  expect(widths.length).toBeGreaterThan(0);
  expect(widths.every((width) => /%$/.test(width))).toBe(true);
  expect(await violations()).toEqual([]);

  // The dashboard's live search (it fetches index shards).
  await page.goto("/atlas");
  await page.getByLabel("Search covered articles").fill("gadg");
  await expect(page.locator("#atlas-count")).toHaveText("1 of 2 articles match.");
  expect(await violations()).toEqual([]);

  await page.goto("/atlas/universe/0e6efcaae05b9aee11c90b919b55052636ee2932901127ea571268dc3a8d6a25");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  expect(await violations()).toEqual([]);

  // The article page's summary box.
  await page.goto("/Widget_lemma");
  await expect(page.getByRole("complementary", { name: "Formalization atlas" })).toContainText("4 passages");
  expect(await violations()).toEqual([]);
  expect(errors).toEqual([]);
});
