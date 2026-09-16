import type { DashboardTab } from "../types/api";

export interface DashboardTabConfig {
  id: DashboardTab;
  label: string;
  panelId: string;
}

export const DASHBOARD_TAB_CONFIGS: DashboardTabConfig[] = [
  { id: "dashboard", label: "Dashboard", panelId: "tab-dashboard" },
  { id: "convert", label: "Convert", panelId: "tab-convert" },
  { id: "document", label: "Document", panelId: "tab-document" },
  { id: "library", label: "Library", panelId: "tab-library" },
  { id: "progress", label: "Progress", panelId: "tab-progress" },
  { id: "logs", label: "Logs", panelId: "tab-logs" },
  { id: "settings", label: "Settings", panelId: "tab-config" },
];

export function conversionPanelTabForDashboardTab(tab: DashboardTab): DashboardTab | "config" {
  return tab === "settings" ? "config" : tab;
}
