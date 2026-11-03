import { useEffect, useState } from "react";

/** Text and code fonts, stored per browser like the theme (a viewer preference, not a server
 * setting). Every font is bundled (main.tsx), so nothing loads from the network, and a font
 * nobody picks is never downloaded. main.tsx applies the stored choice before the first
 * render, while the page is still empty. */
export type FontSlot = "text" | "code";

export interface FontChoice {
  id: string;
  label: string;
  stack: string;
  /** OpenType features to turn on for body text; only Inter has ones we want. */
  features?: string;
}

const SANS_FALLBACK = `"PingFang SC", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`;
const MONO_FALLBACK = `ui-monospace, Menlo, monospace`;

const FIRA = { id: "fira-code", label: "Fira Code", stack: `"Fira Code Variable", "Fira Code", ${MONO_FALLBACK}` };
const JETBRAINS = { id: "jetbrains-mono", label: "JetBrains Mono", stack: `"JetBrains Mono Variable", "JetBrains Mono", ${MONO_FALLBACK}` };
const PLEX_MONO = { id: "ibm-plex-mono", label: "IBM Plex Mono", stack: `"IBM Plex Mono", ${MONO_FALLBACK}` };

export const FONTS: Record<FontSlot, FontChoice[]> = {
  text: [
    { id: "inter", label: "Inter", stack: `"Inter Variable", "Inter", ${SANS_FALLBACK}`, features: `"cv11", "ss01"` },
    { id: "ibm-plex-sans", label: "IBM Plex Sans", stack: `"IBM Plex Sans Variable", "IBM Plex Sans", ${SANS_FALLBACK}` },
    FIRA,
    JETBRAINS,
    PLEX_MONO,
    { id: "system", label: "System sans-serif", stack: `system-ui, ${SANS_FALLBACK}` },
  ],
  code: [
    { id: "roboto-mono", label: "Roboto Mono", stack: `"Roboto Mono", ${MONO_FALLBACK}` },
    FIRA,
    JETBRAINS,
    PLEX_MONO,
    { id: "system", label: "System monospace", stack: MONO_FALLBACK },
  ],
};

const KEY: Record<FontSlot, string> = { text: "orchestrator.font.text", code: "orchestrator.font.code" };
const EVENT = "orchestrator:font";

function choice(slot: FontSlot, id: string | null): FontChoice {
  return FONTS[slot].find((f) => f.id === id) ?? FONTS[slot][0];
}

export function getFont(slot: FontSlot): FontChoice {
  try {
    return choice(slot, localStorage.getItem(KEY[slot]));
  } catch {
    return FONTS[slot][0];
  }
}

function apply(slot: FontSlot, font: FontChoice): void {
  const style = document.documentElement.style;
  if (slot === "text") {
    style.setProperty("--font-sans", font.stack);
    style.setProperty("--font-sans-features", font.features ?? "normal");
  } else {
    style.setProperty("--font-mono", font.stack);
  }
}

export function setFont(slot: FontSlot, id: string): void {
  const font = choice(slot, id);
  try {
    if (font.id === FONTS[slot][0].id) localStorage.removeItem(KEY[slot]);
    else localStorage.setItem(KEY[slot], font.id);
  } catch {
    /* storage blocked: the choice still applies for this page view */
  }
  apply(slot, font);
  window.dispatchEvent(new CustomEvent(EVENT));
}

export function applyStoredFonts(): void {
  apply("text", getFont("text"));
  apply("code", getFont("code"));
}

/** The current choice for a slot; re-renders when it changes. */
export function useFont(slot: FontSlot): [FontChoice, (id: string) => void] {
  const [font, setState] = useState<FontChoice>(() => getFont(slot));
  useEffect(() => {
    const onChange = () => setState(getFont(slot));
    window.addEventListener(EVENT, onChange);
    return () => window.removeEventListener(EVENT, onChange);
  }, [slot]);
  return [font, (id) => setFont(slot, id)];
}
