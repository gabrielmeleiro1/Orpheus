// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";

import { ConversionHelp } from "./ConversionHelp";

it("associates each help button with its explanatory text and preserves the visual tooltip", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const explanations = ["Choose the narration engine.", "Choose a voice for the narration."];
  try {
    await act(async () => root.render(<>
      <ConversionHelp label="Engine help" text={explanations[0]} />
      <ConversionHelp label="Voice help" text={explanations[1]} />
    </>));
    const buttons = [...container.querySelectorAll("button")];
    const ids = buttons.map((button) => button.getAttribute("aria-describedby"));
    expect(new Set(ids).size).toBe(2);
    buttons.forEach((button, index) => {
      const descriptionId = button.getAttribute("aria-describedby");
      expect(descriptionId).toBeTruthy();
      const description = document.getElementById(descriptionId!);
      expect(description?.textContent).toBe(explanations[index]);
      expect(description?.classList.contains("sr-only")).toBe(true);
      expect(button.getAttribute("data-tip")).toBe(explanations[index]);
    });
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
