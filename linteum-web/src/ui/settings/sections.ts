/**
 * Settings-sidebar helpers: which schema sections are editable generically and
 * how the sidebar search matches a section/field.
 */

import type {
  ConfigFieldSpec,
  ConfigFieldValue,
  ConfigSchema,
  ConfigSchemaSection,
} from "../../protocol/commands";
import {
  fieldDescription,
  fieldTitle,
  sectionTitle,
} from "../../state/configuration";
import type { Locale, Strings } from "../../strings";

/**
 * Sections not shown in the web settings sidebar:
 * - agent.llm / agent.profiles: dedicated Providers page;
 * - ui.*: TUI/client display preferences; the web owns theme/locale in its own
 *   General/Appearance pages.
 */
export const EXCLUDED_SECTIONS = new Set([
  "agent.llm",
  "agent.profiles",
  "ui",
]);

/** The plugin runtime page owns this field; it is hidden from generic forms. */
export const EXCLUDED_FIELDS = new Set(["agent.plugins.allow"]);

/**
 * Web-only titles for schema sections whose server title collides with a
 * dedicated page. The Plugins page owns the catalogue; this section edits the
 * plugin runtime settings (dirs/limits/timeouts), not the plugin list.
 */
const SECTION_TITLE_OVERRIDES: Record<
  string,
  { title: string; title_i18n: Record<string, string> }
> = {
  "agent.plugins": {
    title: "插件设置",
    title_i18n: { en: "Plugin settings", "zh-CN": "插件设置" },
  },
};

export const GROUP_KEYS: Record<string, keyof Strings["settings"]> = {
  agent: "configGroupAgent",
  gateway: "configGroupGateway",
  storage: "configGroupStorage",
  logging: "configGroupLogging",
};

export function groupOf(section: ConfigSchemaSection): string {
  return section.key.split(".", 1)[0];
}

export function groupLabel(
  group: string,
  strings: Strings,
): string {
  const key = GROUP_KEYS[group] ?? "configGroupOther";
  return strings.settings[key] as string;
}

/** All generically editable sections, in schema order, with fields filtered. */
export function buildConfigSections(
  schema: ConfigSchema | null,
): ConfigSchemaSection[] {
  return (schema?.sections ?? [])
    .filter((section) => !EXCLUDED_SECTIONS.has(section.key))
    .map((section) => {
      const override = SECTION_TITLE_OVERRIDES[section.key];
      return {
        ...section,
        ...(override
          ? {
              title: override.title,
              title_i18n: { ...section.title_i18n, ...override.title_i18n },
            }
          : {}),
        fields: section.fields.filter((spec) => !EXCLUDED_FIELDS.has(spec.key)),
      };
    });
}

/** Sidebar search: section key/title or any field key/title/description. */
export function sectionMatches(
  section: ConfigSchemaSection,
  query: string,
  locale: Locale,
): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  if (section.key.toLowerCase().includes(needle)) return true;
  if (sectionTitle(section, locale).toLowerCase().includes(needle)) return true;
  return section.fields.some((spec) =>
    spec.key.toLowerCase().includes(needle) ||
    fieldTitle(spec, locale).toLowerCase().includes(needle) ||
    fieldDescription(spec, locale).toLowerCase().includes(needle),
  );
}

/** Active-section search/source filter. */
export function fieldMatches(
  spec: ConfigFieldSpec,
  query: string,
  source: string,
  field: ConfigFieldValue | undefined,
  locale: Locale,
): boolean {
  if (source !== "all" && field?.source !== source) return false;
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return (
    spec.key.toLowerCase().includes(needle) ||
    fieldTitle(spec, locale).toLowerCase().includes(needle) ||
    fieldDescription(spec, locale).toLowerCase().includes(needle)
  );
}
