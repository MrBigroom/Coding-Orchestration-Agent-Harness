import { useEffect, useState } from "react";

/** Light / dark / follow-the-system theme, stored per browser (a viewer preference, not a
 * server setting). index.html applies the stored choice before first paint; keep the key
 * and logic in sync with the inline script there. */
export type ThemePref = "light" | "dark" | "system";

const KEY = "orchestrator.theme";
const EVENT = "orchestrator:theme";
const media = () => window.matchMedia("(prefers-color-scheme: dark)");

export function getThemePref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

function apply(pref: ThemePref): void {
  const dark = pref === "dark" || (pref === "system" && media().matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

export function setThemePref(pref: ThemePref): void {
  try {
    if (pref === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, pref);
  } catch {
    /* storage blocked: the choice still applies for this page view */
  }
  apply(pref);
  window.dispatchEvent(new CustomEvent(EVENT, { detail: pref }));
}

/** Current preference; re-renders on change and follows OS changes while on "system". */
export function useTheme(): [ThemePref, (p: ThemePref) => void] {
  const [pref, setPref] = useState<ThemePref>(getThemePref);
  useEffect(() => {
    const onPref = (e: Event) => setPref((e as CustomEvent<ThemePref>).detail);
    const onOs = () => getThemePref() === "system" && apply("system");
    window.addEventListener(EVENT, onPref);
    const mq = media();
    mq.addEventListener("change", onOs);
    return () => {
      window.removeEventListener(EVENT, onPref);
      mq.removeEventListener("change", onOs);
    };
  }, []);
  return [pref, setThemePref];
}
