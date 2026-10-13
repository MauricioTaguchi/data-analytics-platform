import { useEffect, useRef } from "react";
import { WorkspaceIcon } from "./WorkspaceIcon";

export type WorkspaceSection = "overview" | "datasets" | "transformations" | "dashboards" | "reports" | "monitoring";

const NAV_GROUPS: Array<{ label: string; items: Array<[WorkspaceSection, string]> }> = [
  { label: "WORKSPACE", items: [["overview", "Overview"], ["datasets", "Datasets"], ["transformations", "Transformations"]] },
  { label: "DELIVERY", items: [["dashboards", "Dashboards"], ["reports", "Reports"]] },
  { label: "OPERATIONS", items: [["monitoring", "Monitoring"]] },
];

type Props = { activeSection: WorkspaceSection; mobileOpen: boolean; live: boolean; busy: boolean; onNavigate: (section: WorkspaceSection) => void; onCloseMobile: () => void; onCheckApi: () => void; onConnect: () => void; onDisconnect: () => void };

export function WorkspaceSidebar(props: Props) {
  const sidebarRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!props.mobileOpen) return;
    const sidebar = sidebarRef.current;
    if (!sidebar) return;
    const focusable = () => Array.from(sidebar.querySelectorAll<HTMLButtonElement>("button:not([disabled])"))
      .filter((button) => button.getClientRects().length > 0);
    const focusFirst = () => (sidebar.querySelector<HTMLButtonElement>('[aria-current="page"]') || focusable()[0])?.focus();
    focusFirst();

    const trapTab = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const buttons = focusable();
      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", trapTab);
    return () => {
      document.removeEventListener("keydown", trapTab);
    };
  }, [props.mobileOpen]);

  const navigate = (section: WorkspaceSection) => { props.onNavigate(section); props.onCloseMobile(); };
  const changeConnection = () => {
    props.onCloseMobile();
    if (props.live) props.onDisconnect();
    else props.onConnect();
  };

  return <>
    {props.mobileOpen ? <button className="mobile-nav-backdrop" aria-label="Close menu" tabIndex={-1} onClick={props.onCloseMobile} /> : null}
    <aside ref={sidebarRef} id="workspace-navigation" className={props.mobileOpen ? "sidebar open" : "sidebar"} role={props.mobileOpen ? "dialog" : undefined} aria-modal={props.mobileOpen ? true : undefined} aria-label="Workspace navigation">
      <div className="sidebar-brand">
        <button className="brand" onClick={() => navigate("overview")} aria-label="Go to overview">
          <span className="brand-mark"><svg width="32" height="36" viewBox="0 0 32 36" fill="currentColor" aria-hidden="true" focusable="false"><path d="m16 2 14 8-14 8L2 10 16 2Z" /><path d="m2 17 5-3 9 5 9-5 5 3-14 8L2 17Z" opacity=".8" /><path d="m2 24 5-3 9 5 9-5 5 3-14 8L2 24Z" opacity=".6" /></svg></span>
          <span className="brand-copy"><strong className="brand-title">DataFlow</strong><span className="brand-subtitle">Analytics workspace</span></span>
        </button>
        <button className="sidebar-mobile-close" aria-label="Close navigation" onClick={props.onCloseMobile}><WorkspaceIcon name="close" size={18} /></button>
      </div>
      <nav className="sidebar-nav" aria-label="Workspace">
        {NAV_GROUPS.map((group) => <div className="nav-group" key={group.label}>
          <h2 className="nav-group-label">{group.label}</h2>
          {group.items.map(([section, label]) => <button key={section} className={props.activeSection === section ? "nav-item active" : "nav-item"} aria-current={props.activeSection === section ? "page" : undefined} onClick={() => navigate(section)}>
            <WorkspaceIcon className="nav-item-icon" name={section} /><span className="nav-item-label">{label}</span>
          </button>)}
        </div>)}
      </nav>
      <div className="sidebar-footer">
        <div className="sidebar-footer-heading"><strong>{props.live ? "API WORKSPACE" : "DEMO WORKSPACE"}</strong><p>{props.live ? "Connected data environment" : "Local preview environment"}</p></div>
        <div className="sidebar-environment"><span className={props.live ? "environment-dot is-live" : "environment-dot is-demo"} /><div className="environment-copy"><span className="environment-label">Environment</span><strong className="environment-value">{props.live ? "Authenticated API" : "Local preview"}</strong></div></div>
        <div className="sidebar-actions">
          <button className="sidebar-action sidebar-action-primary" disabled={props.busy} onClick={changeConnection}><WorkspaceIcon name="chevron" size={17} />{props.live ? "Sign out" : "Connect API"}</button>
          <button className="sidebar-action" disabled={props.busy} onClick={props.onCheckApi}><WorkspaceIcon name="monitoring" size={17} />{props.busy ? "Working…" : "Check health"}</button>
        </div>
      </div>
    </aside>
  </>;
}
