import { Monitor, Moon, Palette, Sun } from "lucide-react";
import type { ThemeChoice } from "../../state/types";

export function ThemeIcon({ theme, size = 16 }: { theme: ThemeChoice; size?: number }) {
  if (theme === "light") return <Sun size={size} />;
  if (theme === "mono") return <Palette size={size} />;
  if (theme === "system") return <Monitor size={size} />;
  return <Moon size={size} />;
}
