import { useEffect, useState } from "react";

import { THEME_STORAGE_KEY } from "../features/dashboard/dashboardHelpers";
import type { ThemeMode } from "../types/api";

export function useThemeMode() {
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return stored === "light" ? "light" : "dark";
  });

  useEffect(() => {
    document.body.setAttribute("data-theme", themeMode);
    window.localStorage.setItem(THEME_STORAGE_KEY, themeMode);
  }, [themeMode]);

  return { themeMode, setThemeMode };
}
