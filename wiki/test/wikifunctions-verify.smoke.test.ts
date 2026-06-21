import { describe, it, expect } from "vitest";
import { wikifunctionsVerifyPage } from "../src/wikifunctions-verify.js";

describe("wikifunctions verify explainer page", () => {
  const html = wikifunctionsVerifyPage();
  it("is a complete HTML document", () => {
    expect(html.startsWith("<!doctype html>")).toBe(true);
    expect(html.trimEnd().endsWith("</html>")).toBe(true);
  });
  it("names the two verified theorems verbatim", () => {
    expect(html).toContain("runProgram_eq_coprime");
    expect(html).toContain("runFac_eq_factorial");
  });
  it("shows the deployed Python for both functions, HTML-escaped", () => {
    expect(html).toContain("def Z13701(a, b):");
    expect(html).toContain("a, b = b, a % b"); // no raw < > & to escape here
    expect(html).toContain("def Z13667(n):");
    expect(html).toContain("for i in range(1, n + 1):");
    // the != in the loop must survive as literal text
    expect(html).toContain("while b != 0:");
  });
  it("states the three-layer cross-check results", () => {
    expect(html).toContain("829"); // differential test cases
    expect(html).toContain("1607"); // lean.py coprime
    expect(html).toContain("21"); // lean.py factorial
    expect(html).toContain("0 mismatches");
  });
  it("links the source files on GitHub", () => {
    expect(html).toContain(
      "github.com/Deicyde/WikiLean/blob/main/wikifunctions/lean/Wikifunctions/Python/Z13701.lean",
    );
    expect(html).toContain(
      "github.com/Deicyde/WikiLean/blob/main/wikifunctions/native/leanpy/Main.lean",
    );
    expect(html).toContain("lake build Wikifunctions.Python.Z13701 Wikifunctions.Python.Z13667");
  });
  it("links back to the spec tracker and carries the shared nav", () => {
    expect(html).toContain('<a href="/wikifunctions">');
    expect(html).toContain('<a href="/wikifunctions/verify">How we verify</a>');
  });
  it("carries the site-wide dark-mode pattern (no-FOUC script, toggle, dark CSS)", () => {
    expect(html).toContain('localStorage.getItem("wl-theme")');
    expect(html).toContain("prefers-color-scheme: dark");
    expect(html).toContain("document.documentElement.dataset.theme=t");
    expect(html).toContain('id="wl-theme-toggle"');
    expect(html).toContain('class="wl-theme-toggle"');
    expect(html).toContain('[data-theme="dark"]');
    expect(html).toContain("--paper:#1a1816");
  });
});
