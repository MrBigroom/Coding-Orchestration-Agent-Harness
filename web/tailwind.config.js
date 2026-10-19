// Tailwind v4 reads theme tokens from CSS (`@theme` in src/styles/index.css).
// This file is loaded via `@config` for content globs and is kept for editor tooling.
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
};
