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

export const DASHBOARD_TABS = DASHBOARD_TAB_CONFIGS.map((tab) => tab.id);

export function dashboardTabLabel(tab: DashboardTab): string {
  return DASHBOARD_TAB_CONFIGS.find((config) => config.id === tab)?.label ?? tab;
}

export function dashboardTabPanelId(tab: DashboardTab): string {
  return DASHBOARD_TAB_CONFIGS.find((config) => config.id === tab)?.panelId ?? `tab-${tab}`;
}

export function conversionPanelTabForDashboardTab(tab: DashboardTab): DashboardTab | "config" {
  return tab === "settings" ? "config" : tab;
}

export function DashboardTabIcon({ tab }: { tab: DashboardTab }) {
  switch (tab) {
    case "dashboard":
      return (
        <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <path d="M3 12h4l2-6 4 12 2-6h6" />
        </svg>
      );
    case "convert":
      return (
        <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <path d="M12 5v14M5 12h14" />
        </svg>
      );
    case "document":
      return (
        <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <path d="M7 3h7l4 4v14H7z" />
          <path d="M14 3v5h4M9 12h6M9 16h6" />
        </svg>
      );
    case "library":
      return (
        <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <path d="M5 5h14v14H5zM8 8h8M8 12h8M8 16h5" />
        </svg>
      );
    case "progress":
      return (
        <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <path d="M4 17l5-5 4 3 7-8" />
          <path d="M4 21h16" />
        </svg>
      );
    case "logs":
      return (
        <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <path d="M5 7h14M5 12h14M5 17h9" />
        </svg>
      );
    case "settings":
      return (
        <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
          <path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z" />
          <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" />
        </svg>
      );
    default:
      return null;
  }
}
