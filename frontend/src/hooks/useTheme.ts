import { useCallback, useEffect, useState } from "react";

export type Theme = "dark" | "light";

const THEME_STORAGE_KEY = "dataflow.theme";

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return typeof window !== "undefined" && window.localStorage.getItem(THEME_STORAGE_KEY) === "light" ? "light" : "dark";
    } catch {
      return "dark";
    }
  });

  useEffect(() => {
    if (typeof document !== "undefined") document.documentElement.dataset.theme = theme;
    try {
      if (typeof window !== "undefined") window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // The selected theme remains usable when browser storage is unavailable.
    }
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((current) => current === "dark" ? "light" : "dark");
  }, []);

  return { theme, toggleTheme };
}
