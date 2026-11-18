"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { release } from "../release";

// The landing page's layout. Only the landing page (/v1) changes with it; Temporary runs, Notes, History, Usage and
// Settings look the same in every theme. ``?theme=`` in the URL wins, so a link can show someone a given theme; the
// choice is remembered in this browser otherwise. The release has one theme, Timeline v2, and no switch.
export const THEMES = [["timelinev2", "Timeline v2"], ["zen", "Zen"], ["timeline", "Timeline"], ["full", "Full"], ["debug", "Debug"]] as const;
export type Theme = (typeof THEMES)[number][0];
const KEY = "v1.theme";

export function pickTheme(fromUrl: string | null, stored: string | null): Theme {
  const renamed = (value: string | null) => (value === "pilot" ? "full" : value); // Full was called Pilot at first
  const known = (value: string | null): value is Theme => THEMES.some(([theme]) => theme === value);
  const url = renamed(fromUrl), mine = renamed(stored);
  return known(url) ? url : known(mine) ? mine : "timelinev2";
}

export function useTheme(): [Theme, (theme: Theme) => void] {
  const params = useSearchParams();
  const router = useRouter();
  const [stored, setStored] = useState<string | null>(null);
  useEffect(() => {
    try { setStored(window.localStorage.getItem(KEY)); } catch { /* storage blocked: the URL or the default */ }
  }, []);
  const theme = release() ? "timelinev2" : pickTheme(params.get("theme"), stored);
  const choose = (next: Theme) => {
    try { window.localStorage.setItem(KEY, next); } catch { /* storage blocked: this visit only */ }
    setStored(next);
    router.replace(`/v1?theme=${next}`); // the landing page, where a theme shows
  };
  return [theme, choose];
}

export function ThemeSwitch({ theme, onChange }: { theme: Theme; onChange: (theme: Theme) => void }) {
  return (
    <label className="themeSwitch" title="The landing page's layout; the other pages stay the same">
      <span>Theme</span>
      <select value={theme} onChange={(event) => onChange(event.target.value as Theme)}>
        {THEMES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
    </label>
  );
}

// The switch with its own state, for the top bar of every page: choosing a theme opens the landing page in it.
export function ThemePicker() {
  const [theme, setTheme] = useTheme();
  return release() ? null : <ThemeSwitch theme={theme} onChange={setTheme} />;
}
