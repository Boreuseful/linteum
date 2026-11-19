/** Locale resolution. Pure helpers; the React binding lives in `hooks/useStrings`. */

import type { Locale } from "./strings";

export function localeFromLanguage(language: string | null | undefined): Locale {
  return typeof language === "string" && language.toLowerCase().startsWith("zh")
    ? "zh-CN"
    : "en";
}

/**
 * Prefer a valid stored choice; otherwise follow the browser language.
 *
 * `stored` is the raw localStorage value (possibly absent or corrupted), and
 * `language` is `navigator.language`.
 */
export function resolveLocale(
  stored: string | null,
  language: string | null | undefined,
): Locale {
  if (stored === "en" || stored === "zh-CN") return stored;
  return localeFromLanguage(language);
}
