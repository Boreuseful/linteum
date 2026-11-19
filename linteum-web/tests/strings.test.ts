import { describe, expect, it } from "vitest";
import { localeFromLanguage, resolveLocale } from "../src/i18n";
import { en, zhCN } from "../src/strings";

function keyPaths(value: unknown, prefix = ""): string[] {
  if (value === null || typeof value !== "object") return [prefix];
  return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
    keyPaths(child, prefix ? `${prefix}.${key}` : key),
  );
}

describe("strings", () => {
  it("keeps the Chinese catalogue structurally aligned with English", () => {
    expect(keyPaths(zhCN).sort()).toEqual(keyPaths(en).sort());
  });

  it("resolves the browser language to a supported locale", () => {
    expect(localeFromLanguage("zh-CN")).toBe("zh-CN");
    expect(localeFromLanguage("zh-Hans-CN")).toBe("zh-CN");
    expect(localeFromLanguage("en-US")).toBe("en");
    expect(localeFromLanguage("fr-FR")).toBe("en");
    expect(localeFromLanguage(null)).toBe("en");
  });

  it("prefers a valid stored choice over the browser language", () => {
    expect(resolveLocale("en", "zh-CN")).toBe("en");
    expect(resolveLocale("zh-CN", "en-US")).toBe("zh-CN");
  });

  it("falls back to the browser language when the stored choice is invalid", () => {
    expect(resolveLocale("de", "zh-CN")).toBe("zh-CN");
    expect(resolveLocale(null, "en-GB")).toBe("en");
  });
});
