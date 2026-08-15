import { useState } from "react";
import { authenticate, disconnectApi, getOrCreatePortfolioProject, type AuthenticationInput, type Project } from "../api-client";

export function useAuthentication() {
  const [project, setProject] = useState<Project | null>(null);
  const [busy, setBusy] = useState(false);

  async function connect(input: AuthenticationInput) {
    setBusy(true);
    try {
      await authenticate(input);
      const activeProject = await getOrCreatePortfolioProject();
      setProject(activeProject);
      return activeProject;
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    setBusy(true);
    try { await disconnectApi(); } finally { setProject(null); setBusy(false); }
  }

  return { live: Boolean(project), project, busy, connect, disconnect };
}
