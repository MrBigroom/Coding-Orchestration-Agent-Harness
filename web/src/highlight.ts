import { useEffect, useState } from "react";
import { createHighlighterCore, type HighlighterCore, type ThemedToken } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";
import { bundledLanguages, bundledLanguagesAlias, bundledLanguagesInfo } from "shiki/langs";
import { bundledThemes, bundledThemesInfo } from "shiki/themes";

/** Syntax highlighting with Shiki: VS Code's grammars and themes. Each language and theme is
 * its own chunk, loaded the first time something needs it. Tokens carry both the light and
 * the dark theme's colors as CSS variables (index.css picks one), so switching the app theme
 * needs no new highlighting. The code theme pair is a per-browser preference, like fonts. */

export type Tokens = ThemedToken[][];
type Lang = keyof typeof bundledLanguages;
type Theme = keyof typeof bundledThemes;

// Past these, highlighting a block costs more than it helps; it shows as plain text.
const MAX_CHARS = 150_000;
const MAX_LINES = 3_000;

export const LIGHT_THEMES = bundledThemesInfo.filter((t) => t.type === "light");
export const DARK_THEMES = bundledThemesInfo.filter((t) => t.type === "dark");
export const DEFAULT_THEMES = { light: "github-light", dark: "one-dark-pro" } as const;
export type ThemeMode = keyof typeof DEFAULT_THEMES;

// File extensions Shiki doesn't list as aliases.
const EXTENSIONS: Record<string, Lang> = {
  h: "c", hh: "cpp", hpp: "cpp", cc: "cpp", cxx: "cpp", mjs: "javascript", cjs: "javascript",
  mts: "typescript", cts: "typescript", pyi: "python", htm: "html", kts: "kotlin", lock: "toml",
};

/** A Shiki language id for a fence name, alias or file extension; null when unknown. */
export function resolveLang(name: string | null | undefined): Lang | null {
  const key = name?.trim().toLowerCase();
  if (!key) return null;
  if (key in bundledLanguages) return key as Lang;
  const alias = (bundledLanguagesAlias as Record<string, unknown>)[key];
  if (alias) return bundledLanguagesInfo.find((l) => l.aliases?.includes(key))?.id as Lang ?? null;
  return EXTENSIONS[key] ?? null;
}

export function langLabel(lang: string): string {
  return bundledLanguagesInfo.find((l) => l.id === lang || l.aliases?.includes(lang))?.name ?? lang;
}

/** The language of a file, from its name. */
export function langForPath(path: string | null | undefined): Lang | null {
  const name = path?.split(/[\\/]/).pop()?.toLowerCase() ?? "";
  if (name === "dockerfile" || name.startsWith("dockerfile.")) return "docker";
  if (name === "makefile" || name === "justfile") return "make";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? resolveLang(name.slice(dot + 1)) : null;
}

let core: Promise<HighlighterCore> | null = null;
const loaded = new Set<string>();

async function highlighter(langs: Lang[], themes: Theme[]): Promise<HighlighterCore> {
  core ??= createHighlighterCore({ engine: createJavaScriptRegexEngine(), themes: [], langs: [] });
  const h = await core;
  for (const t of themes) {
    if (!loaded.has(`t:${t}`)) { await h.loadTheme(bundledThemes[t]); loaded.add(`t:${t}`); }
  }
  for (const l of langs) {
    if (!loaded.has(`l:${l}`)) { await h.loadLanguage(bundledLanguages[l]); loaded.add(`l:${l}`); }
  }
  return h;
}

export interface Highlighted { tokens: Tokens; rootStyle: string }

/** Tokens for `code`, or null when the language is unknown or the code too big. */
export async function highlight(code: string, lang: Lang | null, themes: Record<ThemeMode, string>): Promise<Highlighted | null> {
  if (!lang || code.length > MAX_CHARS || code.split("\n", MAX_LINES + 1).length > MAX_LINES) return null;
  const light = themes.light as Theme;
  const dark = themes.dark as Theme;
  const h = await highlighter([lang], [light, dark]);
  const r = h.codeToTokens(code, { lang, themes: { light, dark }, defaultColor: false });
  return { tokens: r.tokens, rootStyle: r.rootStyle || "" };
}

/** "--a:1;--b:2" as a React style object. */
export function styleOf(css: string): Record<string, string> {
  return Object.fromEntries(css.split(";").filter(Boolean).map((d) => {
    const i = d.indexOf(":");
    return [d.slice(0, i).trim(), d.slice(i + 1).trim()];
  }));
}

// ---------------------------------------------------------------- theme preference

const KEY: Record<ThemeMode, string> = { light: "orchestrator.code.light", dark: "orchestrator.code.dark" };
const EVENT = "orchestrator:code-theme";

function stored(mode: ThemeMode): string {
  const list = mode === "light" ? LIGHT_THEMES : DARK_THEMES;
  try {
    const v = localStorage.getItem(KEY[mode]);
    if (v && list.some((t) => t.id === v)) return v;
  } catch {
    /* storage blocked: use the default */
  }
  return DEFAULT_THEMES[mode];
}

export function getCodeThemes(): Record<ThemeMode, string> {
  return { light: stored("light"), dark: stored("dark") };
}

export function setCodeTheme(mode: ThemeMode, id: string): void {
  try {
    if (id === DEFAULT_THEMES[mode]) localStorage.removeItem(KEY[mode]);
    else localStorage.setItem(KEY[mode], id);
  } catch {
    /* storage blocked: the choice still applies for this page view */
  }
  window.dispatchEvent(new CustomEvent(EVENT));
}

/** The chosen light and dark code themes; re-renders when either changes. */
export function useCodeThemes(): Record<ThemeMode, string> {
  const [themes, setThemes] = useState(getCodeThemes);
  useEffect(() => {
    const onChange = () => setThemes(getCodeThemes());
    window.addEventListener(EVENT, onChange);
    return () => window.removeEventListener(EVENT, onChange);
  }, []);
  return themes;
}
