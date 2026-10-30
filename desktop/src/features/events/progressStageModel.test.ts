import { describe, expect, it } from "vitest";

import type { DesktopEventPayload, ProgressStageView } from "../../types/api";
import { buildStageDetail, chapterStageLabel, createProgressStages } from "./progressStageModel";

function stagesWithActive(payload: Partial<DesktopEventPayload>): ProgressStageView[] {
  const detail = buildStageDetail("synthesizing", payload as DesktopEventPayload, 40, "");
  return createProgressStages().map((stage) => (
    stage.name === "synthesis" ? { ...stage, state: "active", pct: 40, detail } : stage
  ));
}

describe("chapterStageLabel", () => {
  it("leads the stage with the chapter being worked on", () => {
    expect(chapterStageLabel(stagesWithActive({ chapter_current: 3, chapter_total: 12 }), "Synthesis"))
      .toBe("Chapter 3/12 · Synthesis");
  });

  it("never reports a chapter past the last one", () => {
    expect(chapterStageLabel(stagesWithActive({ chapter_current: 14, chapter_total: 12 }), "Synthesis"))
      .toBe("Chapter 12/12 · Synthesis");
  });

  it("shows the stage alone when no chapter is reported", () => {
    expect(chapterStageLabel(stagesWithActive({}), "Synthesis")).toBe("Synthesis");
    expect(chapterStageLabel(stagesWithActive({ chunk_current: 2, chunk_total: 9 }), "Synthesis")).toBe("Synthesis");
    expect(chapterStageLabel(stagesWithActive({ chapter_current: 0, chapter_total: 12 }), "Synthesis")).toBe("Synthesis");
    expect(chapterStageLabel(createProgressStages(), "Waiting")).toBe("Waiting");
  });

  it("adds nothing when there is no stage to label", () => {
    expect(chapterStageLabel(stagesWithActive({ chapter_current: 3, chapter_total: 12 }), "")).toBe("");
  });
});
