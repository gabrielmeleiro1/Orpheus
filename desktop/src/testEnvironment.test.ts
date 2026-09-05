// @vitest-environment happy-dom

import { expect, it } from "vitest";

it("uses browser storage in DOM tests on every supported Node version", () => {
  expect(window.localStorage).toBeInstanceOf(Storage);
  expect(window.sessionStorage).toBeInstanceOf(Storage);
  window.localStorage.setItem("environment-check", "local");
  window.sessionStorage.setItem("environment-check", "session");
  expect(window.localStorage.getItem("environment-check")).toBe("local");
  expect(window.sessionStorage.getItem("environment-check")).toBe("session");
  window.localStorage.clear();
  window.sessionStorage.clear();
});
