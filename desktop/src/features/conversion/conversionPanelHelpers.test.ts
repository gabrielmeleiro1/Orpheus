import { describe, expect, it, vi } from "vitest";

import {
  compactSample,
  extractDroppedPaths,
  pathsFromUriList,
  normalizeQuantizationValue,
  nowStatus,
  splitPath,
} from "./conversionPanelHelpers";
import { isTauriRuntime } from "../../platform/tauriRuntime";

describe("conversion panel helpers", () => {
  it("maps backend connection state to status text", () => {
    expect(nowStatus("checking")).toBe("Checking…");
    expect(nowStatus("connected")).toBe("Connected");
    expect(nowStatus("error")).toBe("Error");
  });

  it("detects Tauri runtime from window internals", () => {
    const originalWindow = globalThis.window;
    Object.defineProperty(globalThis, "window", { configurable: true, value: {} });
    const testWindow = globalThis.window as Window & { __TAURI_INTERNALS__?: unknown };

    const descriptor = Object.getOwnPropertyDescriptor(testWindow, "__TAURI_INTERNALS__");
    Reflect.deleteProperty(testWindow, "__TAURI_INTERNALS__");
    expect(isTauriRuntime()).toBe(false);

    Object.defineProperty(testWindow, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    expect(isTauriRuntime()).toBe(true);

    if (descriptor) {
      Object.defineProperty(testWindow, "__TAURI_INTERNALS__", descriptor);
    } else {
      Reflect.deleteProperty(testWindow, "__TAURI_INTERNALS__");
    }
    if (originalWindow) {
      Object.defineProperty(globalThis, "window", { configurable: true, value: originalWindow });
    } else {
      Reflect.deleteProperty(globalThis, "window");
    }
  });

  it("splits unix and windows paths while preserving empty paths", () => {
    expect(splitPath("/tmp/books/source.epub")).toBe("source.epub");
    expect(splitPath("C:\\books\\source.pdf")).toBe("source.pdf");
    expect(splitPath("")).toBe("");
  });

  it("compacts whitespace and truncates long samples", () => {
    expect(compactSample("  hello\n\nworld\t ")).toBe("hello world");
    expect(compactSample("abcdefghijklmnopqrstuvwxyz", 10)).toBe("abcdefg...");
  });

  it("normalizes quantization aliases and arbitrary values", () => {
    expect(normalizeQuantizationValue("")).toBeNull();
    expect(normalizeQuantizationValue(" Q8 ")).toBe("8bit");
    expect(normalizeQuantizationValue("q4")).toBe("4bit");
    expect(normalizeQuantizationValue("FULL")).toBe("bf16");
    expect(normalizeQuantizationValue(" fp16 ")).toBe("fp16");
  });

  it("extracts dropped paths from array, object, and string payloads", () => {
    expect(extractDroppedPaths([" /tmp/a.epub ", "", 4, "/tmp/b.pdf"])).toEqual([
      " /tmp/a.epub ",
      "/tmp/b.pdf",
    ]);
    expect(extractDroppedPaths({ paths: [" /tmp/a.epub ", "", null] })).toEqual([" /tmp/a.epub "]);
    expect(extractDroppedPaths(" /tmp/a.epub ")).toEqual(["/tmp/a.epub"]);
    expect(extractDroppedPaths({ paths: "nope" })).toEqual([]);
  });

  it("parses RFC URI lists without losing Windows UNC paths", () => {
    expect(
      pathsFromUriList([
        "# Finder metadata",
        "file:///Users/me/My%20Book.epub",
        "file://server/share/Book.epub",
        "https://example.com/not-a-file.epub",
        "",
      ].join("\r\n")),
    ).toEqual(["/Users/me/My Book.epub", "//server/share/Book.epub"]);
  });

  it("does not depend on timers", () => {
    vi.useRealTimers();
  });
});
