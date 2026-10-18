import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// The formalization atlas pages over the golden fixture store
// (scripts/atlas/fixtures/store, bound in e2e/server.ts).
const WIDGET = "/atlas/1001/Widget_lemma";

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
    for (const path of [WIDGET, "/atlas", "/atlas/universe/0e6efcaae05b9aee11c90b919b55052636ee2932901127ea571268dc3a8d6a25"]) {
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

test("a passage permalink is not hidden under the sticky header", async ({ page }) => {
  await page.goto(`${WIDGET}#p-5b0e44a1`);
  const header = await page.locator(".wl-header").boundingBox();
  const pill = await page.locator("#p-5b0e44a1 .atlas-pill").boundingBox();
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
