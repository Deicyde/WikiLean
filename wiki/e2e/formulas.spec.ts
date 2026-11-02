import { expect, test, type Page } from "@playwright/test";

// Formulas on an article page, in both shapes of MediaWiki's parse output
// (WP_FORMULA_FIXTURE in test/helpers/fixture.ts, which e2e/server.ts serves as
// /Formula_Article). Each formula arrives as MathML hidden by
// style="display: none;" beside an SVG fallback image. Older pinned bodies
// class the MathML's span mwe-math-mathml-inline or -display; current ones
// class it only mwe-math-mathml-a11y. style.css must draw the MathML, inline in
// the sentence or as a centred block in a display formula, and hide the image.
const FORMULA_PATH = "/Formula_Article";
const SHAPES = ["old", "a11y"] as const;

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

test.beforeEach(async ({ page }) => {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      throw new Error(`browser test attempted an external request: ${url.href}`);
    }
    await route.continue();
  });
});

// The computed display of the one element `selector` matches. (The e2e files
// compile without the DOM lib, so getComputedStyle is typed here.)
async function computedDisplay(page: Page, selector: string): Promise<string> {
  return page.locator(selector).evaluate((element) => {
    const style = Reflect.get(globalThis, "getComputedStyle") as (node: unknown) => { display: string };
    return style(element).display;
  });
}

test("draws each formula's MathML in its sentence or centred on its own line, and hides the fallback image", async ({ page }) => {
  const errors = collectPageErrors(page);
  await page.goto(FORMULA_PATH);
  await expect(page.locator(".wl-article-body .mwe-math-element")).toHaveCount(4);
  for (const shape of SHAPES) {
    for (const kind of ["inline", "display"]) {
      const formula = page.locator(`#formula-${shape}-${kind}`);
      await expect(formula.locator("math"), `${shape} ${kind}`).toBeVisible();
      await expect(formula.locator("img"), `${shape} ${kind}`).toBeHidden();
    }
    // An inline formula follows its lead-in on the same line.
    const inline = page.locator(`#formula-${shape}-inline`);
    const lead = await inline.locator("b").boundingBox();
    const math = await inline.locator("math").boundingBox();
    expect(lead && math, shape).toBeTruthy();
    expect(math!.x, shape).toBeGreaterThanOrEqual(lead!.x + lead!.width - 1);
    expect(math!.y < lead!.y + lead!.height && lead!.y < math!.y + math!.height, shape).toBe(true);
    // A display formula starts below its lead-in, its content centred in the formula's block.
    const display = page.locator(`#formula-${shape}-display`);
    const before = await display.locator("b").boundingBox();
    const block = await display.locator(".mwe-math-element-block").boundingBox();
    const content = await display.locator("math semantics > mrow").boundingBox();
    expect(before && block && content, shape).toBeTruthy();
    expect(block!.y, shape).toBeGreaterThanOrEqual(before!.y + before!.height - 1);
    const offCentre = content!.x + content!.width / 2 - (block!.x + block!.width / 2);
    expect(Math.abs(offCentre), shape).toBeLessThanOrEqual(2);
  }
  // The current shape's span takes its layout from the formula's wrapper.
  expect(await computedDisplay(page, "#formula-a11y-inline .mwe-math-mathml-a11y")).toBe("inline");
  expect(await computedDisplay(page, "#formula-a11y-display .mwe-math-mathml-a11y")).toBe("block");
  expect(errors).toEqual([]);
});
