import { useCallback, useEffect, useRef, useState } from "react";
import { authenticate, disconnectApi, getOrCreatePortfolioProjects, onSessionExpired, requirePositiveId, type AuthenticationInput, type Project } from "../api-client";

export function useAuthentication() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [busy, setBusy] = useState(false);
  const [sessionExpiredAt, setSessionExpiredAt] = useState<number | null>(null);
  const connection = useRef<AbortController | null>(null);

  useEffect(() => onSessionExpired(() => {
    connection.current?.abort();
    setProjects([]);
    setProject(null);
    setSessionExpiredAt(Date.now());
  }), []);
  useEffect(() => () => connection.current?.abort(), []);

  const connect = useCallback(async (input: AuthenticationInput) => {
    connection.current?.abort();
    const controller = new AbortController();
    connection.current = controller;
    setBusy(true);
    try {
      await authenticate(input, controller.signal);
      const available = await getOrCreatePortfolioProjects(controller.signal);
      controller.signal.throwIfAborted();
      setProjects(available);
      setProject(available[0]);
      setSessionExpiredAt(null);
      return available[0];
    } catch (error) {
      if (connection.current === controller) {
        await disconnectApi();
        setProjects([]);
        setProject(null);
      }
      throw error;
    } finally {
      if (connection.current === controller) { connection.current = null; setBusy(false); }
    }
  }, []);

  const selectProject = useCallback((projectId: number) => {
    requirePositiveId(projectId, "Project ID");
    const selected = projects.find((item) => item.id === projectId);
    if (!selected) throw new Error("Choose a project from your workspace list.");
    setProject(selected);
  }, [projects]);

  const disconnect = useCallback(async () => {
    connection.current?.abort();
    connection.current = null;
    setBusy(true);
    try { await disconnectApi(); } finally { setProjects([]); setProject(null); setBusy(false); }
  }, []);

  return { live: Boolean(project), project, projects, selectProject, busy, sessionExpiredAt, connect, disconnect };
}
