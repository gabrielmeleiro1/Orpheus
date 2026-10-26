import { useEffect, type MutableRefObject } from "react";

import type { DashboardTab } from "../types/api";

interface UseGlobalShortcutsArgs {
  enabled?: boolean;
  startEnabled?: boolean;
  activeTab: DashboardTab;
  startCommandRef: MutableRefObject<(() => void) | null>;
  refreshLibrary: () => Promise<void>;
  playRelative: (delta: number) => Promise<void>;
  seekBy: (deltaSec: number) => void;
  togglePlay: () => Promise<void>;
}

export function useGlobalShortcuts({
  enabled = true,
  startEnabled = true,
  activeTab,
  startCommandRef,
  refreshLibrary,
  playRelative,
  seekBy,
  togglePlay,
}: UseGlobalShortcutsArgs): void {
  useEffect(() => {
    if (!enabled) return;
    const onGlobalKeydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing) return;
      const target = event.target as HTMLElement | null;
      const control = target?.closest?.(
        "input, textarea, select, button, summary, a[href], [role='button'], [role='combobox'], [role='listbox']",
      );
      if (control || target?.isContentEditable) {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }

      const key = String(event.key || "").toLowerCase();
      if (event.repeat && key !== "arrowleft" && key !== "arrowright") return;
      if (key === "s") {
        if (startEnabled && activeTab === "convert") {
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
  }, [enabled, startEnabled, activeTab, playRelative, refreshLibrary, seekBy, startCommandRef, togglePlay]);
}
