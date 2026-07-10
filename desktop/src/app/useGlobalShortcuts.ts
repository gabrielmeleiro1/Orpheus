import { useEffect, type MutableRefObject } from "react";

import type { DashboardTab } from "../types/api";

interface UseGlobalShortcutsArgs {
  activeTab: DashboardTab;
  startCommandRef: MutableRefObject<(() => void) | null>;
  refreshLibrary: () => Promise<void>;
  playRelative: (delta: number) => Promise<void>;
  seekBy: (deltaSec: number) => void;
  togglePlay: () => Promise<void>;
}

export function useGlobalShortcuts({
  activeTab,
  startCommandRef,
  refreshLibrary,
  playRelative,
  seekBy,
  togglePlay,
}: UseGlobalShortcutsArgs): void {
  useEffect(() => {
    const onGlobalKeydown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase() || "";
      if (tag === "input" || tag === "textarea" || tag === "button" || target?.isContentEditable) {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }

      const key = String(event.key || "").toLowerCase();
      if (key === "s") {
        if (activeTab === "convert" || activeTab === "document") {
          startCommandRef.current?.();
        }
      } else if (key === "r") {
        void refreshLibrary();
      } else if (key === " ") {
        event.preventDefault();
        void togglePlay();
      } else if (key === "arrowleft") {
        event.preventDefault();
        seekBy(-10);
      } else if (key === "arrowright") {
        event.preventDefault();
        seekBy(10);
      } else if (key === "n") {
        void playRelative(1);
      } else if (key === "p") {
        void playRelative(-1);
      }
    };

    document.addEventListener("keydown", onGlobalKeydown);
    return () => {
      document.removeEventListener("keydown", onGlobalKeydown);
    };
  }, [activeTab, playRelative, refreshLibrary, seekBy, startCommandRef, togglePlay]);
}
