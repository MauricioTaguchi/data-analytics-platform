import { useCallback, useEffect, useState } from "react";
import { authenticate, disconnectApi, getOrCreatePortfolioProject, onSessionExpired, type AuthenticationInput, type Project } from "../api-client";

export function useAuthentication() {
  const [project, setProject] = useState<Project | null>(null);
  const [busy, setBusy] = useState(false);
  const [sessionExpiredAt, setSessionExpiredAt] = useState<number | null>(null);

  useEffect(() => onSessionExpired(() => {
    setProject(null);
    setSessionExpiredAt(Date.now());
  }), []);

  const connect = useCallback(async (input: AuthenticationInput) => {
    setBusy(true);
    try {
      await authenticate(input);
      const activeProject = await getOrCreatePortfolioProject();
      setProject(activeProject);
      setSessionExpiredAt(null);
      return activeProject;
    } catch (error) {
      await disconnectApi();
      setProject(null);
      throw error;
    } finally {
      setBusy(false);
    }
  }, []);

  const disconnect = useCallback(async () => {
    setBusy(true);
    try { await disconnectApi(); } finally { setProject(null); setBusy(false); }
  }, []);

  return { live: Boolean(project), project, busy, sessionExpiredAt, connect, disconnect };
}
