import { useCallback, useEffect, useRef, useState } from "react";
import { authenticate, createProject, disconnectApi, getOrCreatePortfolioProjects, listProjects, onSessionExpired, requirePositiveId, type AuthenticationInput, type Project } from "../api-client";

export function useAuthentication() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [signedIn, setSignedIn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sessionExpiredAt, setSessionExpiredAt] = useState<number | null>(null);
  const connection = useRef<AbortController | null>(null);

  useEffect(() => onSessionExpired(() => {
    connection.current?.abort();
    setProjects([]);
    setProject(null);
    setSignedIn(false);
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
      const available = input.intent === "register"
        ? await getOrCreatePortfolioProjects(controller.signal)
        : await listProjects(controller.signal);
      controller.signal.throwIfAborted();
      setProjects(available);
      setProject(available[0] ?? null);
      setSignedIn(true);
      setSessionExpiredAt(null);
      return available[0] ?? null;
    } catch (error) {
      if (connection.current === controller) {
        await disconnectApi();
        setProjects([]);
        setProject(null);
        setSignedIn(false);
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
    try { await disconnectApi(); } finally { setProjects([]); setProject(null); setSignedIn(false); setBusy(false); }
  }, []);

  const forgetProject = useCallback((projectId: number) => {
    const remaining = projects.filter((item) => item.id !== projectId);
    setProjects(remaining);
    if (project?.id === projectId) setProject(remaining[0] ?? null);
  }, [projects, project]);

  const createWorkspace = useCallback(async () => {
    connection.current?.abort();
    const request = new AbortController();
    connection.current = request;
    try {
      const created = await createProject("New project", request.signal);
      request.signal.throwIfAborted();
      setProjects((available) => [...available, created]);
      setProject(created);
      return created;
    } finally { if (connection.current === request) connection.current = null; }
  }, []);

  const reloadProjects = useCallback(async () => {
    connection.current?.abort();
    const request = new AbortController();
    connection.current = request;
    try {
      const available = await listProjects(request.signal);
      request.signal.throwIfAborted();
      setProjects(available);
      setProject((current) => available.find((item) => item.id === current?.id) ?? available[0] ?? null);
      return available;
    } finally { if (connection.current === request) connection.current = null; }
  }, []);

  return { live: signedIn, project, projects, selectProject, forgetProject, createWorkspace, reloadProjects, busy, sessionExpiredAt, connect, disconnect };
}
