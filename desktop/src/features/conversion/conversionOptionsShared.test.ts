import { describe, expect, it } from "vitest";

import { normalizeSpeedInput, parseSpeed, resolveOptionValue } from "./conversionOptionsShared";

describe("resolveOptionValue", () => {
  const options = [{ value: "first" }, { value: "saved" }, { value: "default" }, { value: "" }];

  it.each([
    ["first", ["saved", "default"], "first"],
    ["", ["saved"], ""],
    ["missing", [" saved ", "default"], "saved"],
    ["missing", ["unavailable", " default "], "default"],
    ["missing", [undefined, null, "", "  "], "first"],
    [" saved ", [], "first"],
  ])("resolves %j with fallbacks %j to %j", (current, fallbacks, expected) => {
    expect(resolveOptionValue(options, current, ...fallbacks)).toBe(expected);
  });

  it("returns an empty value when no options are available", () => {
    expect(resolveOptionValue([], "saved", "default")).toBe("");
  });
});

describe("parseSpeed", () => {
  it("falls back to 1 for non-positive or non-numeric input", () => {
    expect(parseSpeed("-999")).toBe(1);
    expect(parseSpeed("0")).toBe(1);
    expect(parseSpeed("")).toBe(1);
    expect(parseSpeed("abc")).toBe(1);
  });

  it("clamps an in-range value through untouched", () => {
    expect(parseSpeed("2.5")).toBe(2.5);
    expect(parseSpeed("1")).toBe(1);
  });

  it("clamps values above the maximum to 3", () => {
    expect(parseSpeed("99")).toBe(3);
    expect(parseSpeed("3.01")).toBe(3);
  });

  it("clamps positive values below the minimum to 0.5", () => {
    expect(parseSpeed("0.1")).toBe(0.5);
    expect(parseSpeed("0.49")).toBe(0.5);
  });
});

describe("normalizeSpeedInput", () => {
  it("makes the displayed value match the effective bounded speed", () => {
    expect(normalizeSpeedInput("999")).toBe("3");
    expect(normalizeSpeedInput("0.1")).toBe("0.5");
    expect(normalizeSpeedInput("-4")).toBe("1");
    expect(normalizeSpeedInput("not-a-number")).toBe("1");
    expect(normalizeSpeedInput("1.25")).toBe("1.25");
  });
});
