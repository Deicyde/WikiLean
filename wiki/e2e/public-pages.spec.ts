import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      throw new Error(`browser test attempted an external request: ${url.href}`);
    }
    await route.continue();
  });
});

for (const colorScheme of ["light", "dark"] as const) {
  test(`public entry pages are readable on a phone in ${colorScheme} mode`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.emulateMedia({ colorScheme });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    for (const path of ["/", "/contribute", "/about"]) {
      await page.goto(path);
      await expect(page.getByRole("main")).toBeVisible();
      expect(await page.locator("html").evaluate((el) => el.scrollWidth - el.clientWidth)).toBe(0);
      const audit = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
      expect(audit.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
    }
    expect(errors).toEqual([]);
  });
}

test("a new visitor can reach an article and find the contribution instructions", async ({ page }) => {
  const graphRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/brain")) graphRequests.push(request.url());
  });
  await page.goto("/");
  await page.getByRole("link", { name: "Browse articles", exact: true }).click();
  await expect(page.locator("#articles-q")).toBeVisible();
  await expect(page.locator("#articles-about")).not.toHaveAttribute("open");
  await expect(page.getByRole("region", { name: "Table: By library" })).toBeHidden();
  await page.locator("#articles-list").getByRole("link", { name: "Widget lemma", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Widget lemma");
  await page.locator("header").getByRole("link", { name: "Contribute", exact: true }).click();
  await expect(page.getByRole("link", { name: "Manage agents", exact: true })).toHaveAttribute("href", "/agents");
  await expect(page.getByRole("link", { name: "Sign in", exact: true })).toHaveAttribute("href", "/login");
  expect(graphRequests).toEqual([]);
});

test("an owner can enable laptop contributions without editing an article", async ({ page, context, baseURL }) => {
  await context.addCookies([{ name: "wl_dev_user", value: "u-patroller", url: baseURL! }]);
  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: "Enable contributions" })).toBeVisible();
  await page.getByRole("button", { name: "Accept contribution terms" }).click();
  await expect(page.getByRole("status")).toHaveText("Read and check the agreement first.");
  await page.locator("#cc0-agreement").check();
  await page.getByRole("button", { name: "Accept contribution terms" }).click();
  await expect(page.getByText("You have accepted the CC0 contribution agreement.", { exact: false })).toBeVisible();
  await expect(page.getByRole("link", { name: "Submit pipeline results from your laptop" })).toBeVisible();
});
