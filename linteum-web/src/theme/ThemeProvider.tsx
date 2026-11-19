import { useEffect, type ReactNode } from "react";
import { useAppState } from "../hooks/useStore";
import type { ThemeChoice } from "../state/types";

function resolveTheme(choice: ThemeChoice): "dark" | "light" | "mono" {
  if (choice !== "system") return choice;
  if (typeof window === "undefined" || !window.matchMedia) return "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** Applies the selected theme to `<html data-theme>`. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const state = useAppState();
  const choice = state.ui.theme;

  useEffect(() => {
    const apply = () => {
      document.documentElement.dataset.theme = resolveTheme(choice);
    };
    apply();
    if (choice !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: light)");
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [choice]);

  return <>{children}</>;
}
