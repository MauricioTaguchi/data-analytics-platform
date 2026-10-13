import type { ReactNode } from "react";

export type WorkspaceIconName = "overview" | "datasets" | "transformations" | "dashboards" | "reports" | "monitoring" | "sun" | "moon" | "upload" | "download" | "menu" | "close" | "chevron" | "check" | "search" | "rows" | "columns";

const ICONS: Record<WorkspaceIconName, ReactNode> = {
  overview: <><path d="m3 10 9-7 9 7" /><path d="M5 9v11h5v-7h4v7h5V9" /></>,
  datasets: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>,
  transformations: <><rect x="9" y="2" width="6" height="6" rx="2" /><rect x="2" y="16" width="6" height="6" rx="2" /><rect x="16" y="16" width="6" height="6" rx="2" /><path d="M12 8v4M5 16v-4h14v4" /></>,
  dashboards: <><rect x="3" y="13" width="4" height="8" rx="1" /><rect x="10" y="3" width="4" height="18" rx="1" /><rect x="17" y="8" width="4" height="13" rx="1" /></>,
  reports: <><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Z" /><path d="M14 3v6h6M8 13h8M8 17h6" /></>,
  monitoring: <path d="M2 12h4l3-8 5 16 3-8h5" />,
  sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5" /></>,
  moon: <path d="M20.5 13.3A8.5 8.5 0 0 1 10.7 3.5a8.5 8.5 0 1 0 9.8 9.8Z" />,
  upload: <><path d="M12 16V3m-4 4 4-4 4 4" /><path d="M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5" /></>,
  download: <><path d="M12 3v13m-4-4 4 4 4-4" /><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" /></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  chevron: <path d="m9 5 7 7-7 7" />,
  check: <path d="m5 12 4 4L19 6" />,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
  rows: <><rect x="3" y="3" width="18" height="18" rx="3" /><path d="M3 9h18M3 15h18" /></>,
  columns: <><rect x="3" y="3" width="18" height="18" rx="3" /><path d="M9 3v18M15 3v18" /></>,
};

type Props = { name: WorkspaceIconName; size?: number; className?: string; strokeWidth?: number };

export function WorkspaceIcon({ name, size = 20, className, strokeWidth = 1.7 }: Props) {
  return <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{ICONS[name]}</svg>;
}
