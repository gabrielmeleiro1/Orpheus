import type { DashboardTab } from "../types/api";
import type { IconName } from "../ui/icons";

export interface DashboardTabConfig {
  id: DashboardTab;
  label: string;
  panelId: string;
  description: string;
  icon: IconName;
  group: string;
}

export const DASHBOARD_TAB_CONFIGS: DashboardTabConfig[] = [
  { id: "dashboard", label: "Dashboard", panelId: "tab-dashboard", group: "Create", icon: "dashboard", description: "Your library, current playback, and local services at a glance." },
  { id: "convert", label: "Convert", panelId: "tab-convert", group: "Create", icon: "convert", description: "Turn a book into a chaptered M4B audiobook." },
  { id: "document", label: "Document", panelId: "tab-document", group: "Create", icon: "document", description: "Turn a document or pasted text into an M4A listening copy." },
  { id: "library", label: "Library", panelId: "tab-library", group: "Listen", icon: "library", description: "Browse, play, and manage your converted audiobooks." },
  { id: "progress", label: "Progress", panelId: "tab-progress", group: "Activity", icon: "progress", description: "Follow the running conversion stage by stage." },
  { id: "logs", label: "Logs", panelId: "tab-logs", group: "Activity", icon: "logs", description: "Events streamed from the conversion pipeline." },
  { id: "settings", label: "Settings", panelId: "tab-config", group: "System", icon: "settings", description: "Choose how Orpheus prepares text before narration." },
];

export function conversionPanelTabForDashboardTab(tab: DashboardTab): DashboardTab | "config" {
  return tab === "settings" ? "config" : tab;
}
