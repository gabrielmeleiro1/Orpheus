import type { DashboardTab } from "../types/api";
import type { IconName } from "../ui/icons";

interface DashboardTabConfig {
  id: DashboardTab;
  label: string;
  panelId: string;
  icon: IconName;
}

// Three places: listen (Library), make (Create: queue, progress and log in one
// page) and configure (Settings, including model downloads).
export const DASHBOARD_TAB_CONFIGS: DashboardTabConfig[] = [
  { id: "library", label: "Library", panelId: "tab-library", icon: "library" },
  { id: "convert", label: "Create", panelId: "tab-convert", icon: "convert" },
  { id: "settings", label: "Settings", panelId: "tab-config", icon: "settings" },
];

export function conversionPanelTabForDashboardTab(tab: DashboardTab): DashboardTab | "config" {
  return tab === "settings" ? "config" : tab;
}
