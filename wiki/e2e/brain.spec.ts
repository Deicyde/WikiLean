import { expect, test } from "@playwright/test";

test("the Brain page verifies and selects the local fixture release", async ({ page, request }) => {
  const external: string[] = [];
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      external.push(url.href);
      await route.abort();
      return;
    }
    await route.continue();
  });
  const releaseReads: string[] = [];
  page.on("requestfinished", (finished) => {
    const { pathname } = new URL(finished.url());
    if (pathname.startsWith("/assets/brain/")) releaseReads.push(pathname);
  });
  const selector = await (await request.get("/assets/brain/current.json")).json();
  const releaseBase = `/assets/brain/releases/${selector.manifest_sha256}`;

  await page.goto("/brain");

  // boot() writes #release-id only after the selector, the manifest's digest and
  // self-identity, and the cell manifest's declared bytes all verify. (#status is
  // no witness: the first render overwrites it within milliseconds.)
  await expect(page.locator("#release-id")).toHaveText(`release ${selector.release.slice(0, 12)}`);
  await expect(page.locator("#release-id")).toHaveAttribute("title", selector.release_id);
  await expect.poll(() => releaseReads).toEqual(expect.arrayContaining([
    "/assets/brain/current.json",
    `${releaseBase}/release.json`,
    `${releaseBase}/cells/manifest.json`,
  ]));
  expect(external).toEqual([]);
});
