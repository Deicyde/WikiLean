import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const editorPath = fileURLToPath(new URL("../assets/editor.js", import.meta.url));
const source = readFileSync(editorPath, "utf8");

function functionBody(name: string): string {
  const start = source.indexOf(`function ${name}(`);
  expect(start, `${name} should exist`).toBeGreaterThanOrEqual(0);

  const brace = source.indexOf("{", start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(brace + 1, i);
  }
  throw new Error(`could not find the end of ${name}`);
}

describe("editor dialog focus source contract", () => {
  it("captures a stable opener before moving focus into either dialog mode", () => {
    const editBody = functionBody("openEditor");
    const addBody = functionBody("openAdder");

    for (const body of [editBody, addBody]) {
      expect(body).toContain("rememberDialogOpener(opener);");
      expect(body.indexOf("rememberDialogOpener(opener);")).toBeLessThan(
        body.indexOf('panel.classList.add("open")'),
      );
      expect(body.indexOf('panel.classList.add("open")')).toBeLessThan(
        body.indexOf('$("wlr-f-status").focus()'),
      );
    }

    const rememberBody = functionBody("rememberDialogOpener");
    expect(rememberBody).toContain('panel.classList.contains("open")');
    expect(rememberBody).toContain("document.activeElement");
    expect(rememberBody).toContain("defaultDialogFallback()");
  });

  it("traps Tab and Shift+Tab among visible enabled dialog controls", () => {
    const focusableBody = functionBody("focusableDialogControls");
    expect(focusableBody).toContain("button,input,select,textarea");
    expect(focusableBody).toContain("el.disabled");
    expect(focusableBody).toContain('style.display !== "none"');
    expect(focusableBody).toContain('style.visibility !== "hidden"');
    expect(focusableBody).toContain("el.getClientRects().length > 0");

    const trapBody = functionBody("trapDialogTab");
    expect(trapBody).toContain("e.shiftKey");
    expect(trapBody).toContain("last.focus()");
    expect(trapBody).toContain("first.focus()");
    expect(source).toContain('e.key === "Tab" && panel.classList.contains("open")');
    expect(source).toContain("trapDialogTab(e);");
  });

  it("restores focus after close and never targets picker rows or the hidden FAB", () => {
    const stableBody = functionBody("isStableFocusTarget");
    expect(stableBody).toContain("el.isConnected");
    expect(stableBody).toContain('!el.closest("#wlr-picker")');
    expect(stableBody).toContain("el !== fab");

    const closeBody = functionBody("closePanel");
    expect(closeBody).toContain('panel.classList.remove("open")');
    expect(closeBody).toContain("restoreDialogFocus();");
    expect(closeBody.indexOf('panel.classList.remove("open")')).toBeLessThan(
      closeBody.indexOf("restoreDialogFocus();"),
    );

    expect(source).toContain("openEditor(i, annoEl);");
    expect(source).toContain("openAdder(pendingSel.text, pendingSel.section, articleBody);");
    expect(source.match(/openEditor\([^\n]+, e\.currentTarget\);/g)).toHaveLength(3);
  });

  it("preserves Escape, save shortcut, and dirty-close confirmation behavior", () => {
    expect(source).toMatch(
      /if \(e\.key === "Escape"\) \{[\s\S]*?if \(panel\.classList\.contains\("open"\)\) requestClose\(\);/,
    );
    expect(source).toMatch(
      /\(e\.metaKey \|\| e\.ctrlKey\) && e\.key === "Enter"[\s\S]*?e\.preventDefault\(\);[\s\S]*?save\(\);/,
    );

    const requestCloseBody = functionBody("requestClose");
    expect(requestCloseBody).toContain('confirm("Discard unsaved changes?")');
    expect(requestCloseBody).toContain("closePanel();");
    expect(source).toContain('$("wlr-close").addEventListener("click", requestClose);');
  });
});
