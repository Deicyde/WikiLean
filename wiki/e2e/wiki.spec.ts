import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const ARTICLE_PATH = "/Test_Article";

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

async function rejectExternalRequests(page: Page): Promise<void> {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      throw new Error(`browser test attempted an external request: ${url.href}`);
    }
    await route.continue();
  });
}

test.beforeEach(async ({ page }) => {
  await rejectExternalRequests(page);
});

test("renders the seeded article without serious accessibility violations", async ({ page }) => {
  const errors = collectPageErrors(page);
  await page.goto(ARTICLE_PATH);

  await expect(page).toHaveTitle("WikiLean · Test Article");
  await expect(page.getByRole("main")).toContainText("abelian group");
  await expect(page.locator(".anno")).toHaveCount(2);
  await expect(page.getByRole("link", { name: "Sign in to edit" })).toHaveAttribute(
    "href",
    "/login?returnTo=%2FTest_Article",
  );

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const seriousViolations = results.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );
  expect(seriousViolations).toEqual([]);
  expect(errors).toEqual([]);
});

test("returns to the article after development login", async ({ page }) => {
  const errors = collectPageErrors(page);
  await page.goto(ARTICLE_PATH);
  await page.getByRole("link", { name: "Sign in to edit" }).click();

  await expect(page).toHaveURL(/\/Test_Article$/);
  await expect(page.locator("#wlr-bar")).toContainText("editing as Dev User");
  await expect(page.getByRole("link", { name: "Sign in to edit" })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("rejects a hostile login return target without leaving loopback", async ({ page }) => {
  await page.goto(`/login?returnTo=${encodeURIComponent("javascript:globalThis.__wlInjected=true")}`);

  await expect(page).toHaveURL(/\/$/);
  await expect(page).toHaveTitle(/WikiLean/);
  expect(new URL(page.url()).hostname).toBe("127.0.0.1");
  expect(await page.evaluate(() => Reflect.get(globalThis, "__wlInjected"))).toBeUndefined();
});

test("persists an explicit theme selection across reload", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(ARTICLE_PATH);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  await page.getByRole("button", { name: "Toggle dark mode" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("wl-theme"))).toBe("dark");

  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});

test("moves, traps, and restores focus for the annotation editor", async ({ page }) => {
  await page.goto(`/login?returnTo=${encodeURIComponent(ARTICLE_PATH)}`);
  await expect(page).toHaveURL(/\/Test_Article$/);
  await page.evaluate(() => localStorage.setItem("wl_editor_intro_seen", "1"));
  await page.reload();

  const highlight = page.locator(".anno").first();
  await highlight.click();

  const dialog = page.getByRole("dialog", { name: /Edit: Abelian group/ });
  await expect(dialog).toBeVisible();
  await expect(page.getByLabel("Status", { exact: true })).toBeFocused();

  const close = page.getByRole("button", { name: "Close editor" });
  await close.focus();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Delete" })).toBeFocused();

  const label = page.getByLabel("Label", { exact: true });
  await label.fill("Unsaved label");
  page.once("dialog", async (confirmation) => confirmation.dismiss());
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(label).toBeFocused();
  await expect(label).toHaveValue("Unsaved label");

  await label.fill("Abelian group");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(highlight).toBeFocused();
});
