export type WorkspaceSection = "overview" | "datasets" | "transformations" | "dashboards" | "reports" | "monitoring";
const NAV_ITEMS: Array<[WorkspaceSection, string, string]> = [
  ["overview", "Overview", "01"], ["datasets", "Datasets", "02"], ["transformations", "Transformations", "03"],
  ["dashboards", "Dashboards", "04"], ["reports", "Reports", "05"], ["monitoring", "Monitoring", "06"],
];
type Props = { activeSection: WorkspaceSection; mobileOpen: boolean; live: boolean; busy: boolean; onNavigate: (section: WorkspaceSection) => void; onCloseMobile: () => void; onCheckApi: () => void; onConnect: () => void; onDisconnect: () => void };
export function WorkspaceSidebar(props: Props) {
  const navigate = (section: WorkspaceSection) => { props.onNavigate(section); props.onCloseMobile(); };
  return <aside className={props.mobileOpen ? "sidebar open" : "sidebar"}>
    <button className="brand" onClick={() => navigate("overview")} aria-label="Go to overview"><span className="brand-mark">⌁</span><span>DataFlow</span></button>
    <nav aria-label="Primary navigation">{NAV_ITEMS.map(([section, label, number]) => <button key={section} className={props.activeSection === section ? "nav-item active" : "nav-item"} onClick={() => navigate(section)}><span>{number}</span>{label}</button>)}</nav>
    <div className="sidebar-footer"><span>Environment</span><strong>{props.live ? "Authenticated API" : "Local demo"}</strong>
      <button disabled={props.busy} onClick={props.live ? props.onDisconnect : props.onConnect}>{props.live ? "Sign out" : "Connect API"}</button>
      <button disabled={props.busy} onClick={props.onCheckApi}>{props.busy ? "Working…" : "Check health"}</button></div>
  </aside>;
}
