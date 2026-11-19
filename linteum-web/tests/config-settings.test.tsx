import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it } from "vitest";
import type { ConfigFieldSpec, ConfigFieldValue } from "../src/protocol/commands";
import { store } from "../src/state/instance";
import { sectionTitle } from "../src/state/configuration";
import { en } from "../src/strings";
import {
  ConfigFieldControl,
  type ConfigFieldControlProps,
} from "../src/ui/settings/ConfigFieldControl";
import { ConfigSettingsSection } from "../src/ui/settings/ConfigSettingsSection";
import {
  buildConfigSections,
  fieldMatches,
  sectionMatches,
} from "../src/ui/settings/sections";
import { SettingsPage } from "../src/ui/layout/SettingsPage";

function spec(overrides: Partial<ConfigFieldSpec>): ConfigFieldSpec {
  return {
    key: "agent.context.max_tokens",
    type: "int",
    default: 32000,
    title: "Context budget",
    description: "",
    title_i18n: {},
    description_i18n: {},
    enum: [],
    minimum: null,
    maximum: null,
    secret: false,
    advanced: false,
    apply_scope: "new_session",
    nullable: false,
    allow_custom: false,
    section: "agent.context",
    item_type: "",
    item_section: "",
    ...overrides,
  };
}

function field(overrides: Partial<ConfigFieldValue>): ConfigFieldValue {
  return {
    value: 32000,
    source: "file",
    secret: false,
    set: true,
    masked: null,
    ...overrides,
  };
}

function renderField(
  fieldSpec: ConfigFieldSpec,
  fieldValue: ConfigFieldValue,
  extra: Partial<ConfigFieldControlProps> = {},
): string {
  return renderToStaticMarkup(
    <ConfigFieldControl
      spec={fieldSpec}
      field={fieldValue}
      value={fieldValue.value}
      locale="en"
      strings={en}
      scope="new_session"
      onChange={() => undefined}
      {...extra}
    />,
  );
}

beforeEach(() => {
  store.dispatch({ type: "ui/locale", locale: "en" });
  store.dispatch({ type: "configuration/reset" });
});

describe("ConfigFieldControl", () => {
  it("renders an enum control and source/scope badges", () => {
    const html = renderField(
      spec({
        key: "agent.context.policy",
        type: "enum",
        enum: ["passthrough", "compacting"],
        title: "Context policy",
      }),
      field({ value: "compacting", source: "file" }),
    );
    expect(html).toContain("Context policy");
    expect(html).toContain('<select class="input"');
    expect(html).toContain("compacting");
    expect(html).toContain("passthrough");
    expect(html).toContain(en.settings.configSourceFile);
    expect(html).toContain(en.settings.configScopeNewSession);
  });

  it("keeps secrets masked and offers replace/clear", () => {
    const html = renderField(
      spec({
        key: "agent.llm.api_key",
        type: "secret",
        secret: true,
        nullable: true,
        title: "API Key",
      }),
      field({ value: null, source: "file", secret: true, masked: "sk-…abcd" }),
      { value: undefined },
    );
    expect(html).toContain("sk-…abcd");
    expect(html).toContain(en.settings.configSecretReplace);
    expect(html).toContain(en.settings.configSecretClear);
  });

  it("renders scalar lists as rows with add/remove controls", () => {
    const html = renderField(
      spec({
        key: "agent.tools.auto_approve",
        type: "list",
        item_type: "str",
        title: "Auto approve",
      }),
      field({ value: ["read_file", "list_dir"] }),
    );
    expect(html).toContain("read_file");
    expect(html).toContain("list_dir");
    expect(html).toContain(en.settings.configListAdd);
  });

  it("shows composite values read-only", () => {
    const html = renderField(
      spec({ key: "agent.profiles", type: "map", title: "Profiles" }),
      field({ value: { fast: { model: "x" } } }),
    );
    expect(html).toContain("Profiles");
    expect(html).toContain("&quot;fast&quot;");
    expect(html).toContain(en.settings.configCompositeReadonly);
  });

  it("locks env-owned fields and names the variable", () => {
    const html = renderField(
      spec({ key: "logging.level", type: "enum", enum: ["info", "debug"], title: "Level" }),
      field({ value: "debug", source: "env", env_var: "LINTEUM_LOGGING__LEVEL" }),
    );
    expect(html).toContain("LINTEUM_LOGGING__LEVEL");
    expect(html).toContain("disabled");
    expect(html).toContain(en.settings.configEnvLockedBy.replace("{envVar}", "LINTEUM_LOGGING__LEVEL"));
  });

  it("shows the raw file value when interpolation changed it", () => {
    const html = renderField(
      spec({ key: "logging.path", type: "path", title: "Log path" }),
      field({
        value: "/var/log/linteum.log",
        raw_value: "${LOG_DIR}/linteum.log",
        source: "file",
      }),
    );
    expect(html).toContain("${LOG_DIR}/linteum.log");
    expect(html).toContain("/var/log/linteum.log");
    expect(html).toContain("File value:");
  });
});

describe("ConfigSettingsSection", () => {
  const sectionSpec = spec({
    key: "agent.context.max_tokens",
    title: "Context budget",
    title_i18n: { en: "Context budget" },
  });
  const schema = {
    version: 1,
    sections: [
      {
        key: "agent.llm",
        title: "LLM section",
        title_i18n: { en: "LLM section" },
        fields: [spec({ key: "agent.llm.model", title: "Model", section: "agent.llm" })],
      },
      {
        key: "agent.profiles",
        title: "Profiles section",
        title_i18n: { en: "Profiles section" },
        fields: [spec({ key: "agent.profiles.fast.model", title: "Profile model", section: "agent.profiles" })],
      },
      {
        key: "agent.context",
        title: "Context",
        title_i18n: { en: "Context" },
        fields: [sectionSpec],
      },
      {
        key: "agent.plugins",
        title: "Plugins",
        title_i18n: { en: "Plugins" },
        fields: [
          spec({ key: "agent.plugins.dirs", title: "Plugin dirs", section: "agent.plugins" }),
          spec({ key: "agent.plugins.allow", title: "Allowed plugins", section: "agent.plugins" }),
        ],
      },
      {
        key: "ui",
        title: "Interface section",
        title_i18n: { en: "Interface section" },
        fields: [spec({ key: "ui.theme", title: "Theme", section: "ui" })],
      },
    ],
  };

  function seed(features: string[]): void {
    store.dispatch({
      type: "gateway/hello",
      hello: {
        instance_id: "instance-1",
        protocol: 1,
        features,
        limits: {},
      },
    });
    store.dispatch({
      type: "configuration/loaded",
      result: {
        revision: "rev-1",
        path: "/tmp/config.yaml",
        exists: true,
        manage: true,
        writable: true,
        schema,
        fields: {
          "agent.context.max_tokens": field({ value: 32000 }),
        },
        runtimeScopes: { "agent.context.max_tokens": "new_session" },
        issues: [],
      },
      sections: ["agent.context"],
      fieldsIncluded: true,
    });
  }

  it("hides the page when the gateway lacks config.settings", () => {
    seed([]);
    const html = renderToStaticMarkup(<ConfigSettingsSection sectionKey="agent.context" />);
    expect(html).toContain(en.settings.configFeatureMissing);
  });

  it("lists config sections in the settings sidebar and filters excluded ones", () => {
    seed(["config.settings"]);
    const html = renderToStaticMarkup(<SettingsPage />);
    expect(html).toContain(en.settings.searchAll);
    expect(html).toContain(en.settings.groupClient);
    expect(html).toContain(en.settings.configGroupAgent);
    expect(html).toContain("Context");
    expect(html).toContain("Plugins");
    expect(html).not.toContain(en.settings.configuration);
    expect(html).not.toContain("LLM section");
    expect(html).not.toContain("Profiles section");
    expect(html).not.toContain("Allowed plugins");
    expect(html).not.toContain("Interface section");
  });

  it("groups the LLM and Plugins pages under Agent", () => {
    seed(["config.settings"]);
    const html = renderToStaticMarkup(<SettingsPage />);
    const client = html.indexOf(`>${en.settings.groupClient}<`);
    const agent = html.indexOf(`>${en.settings.configGroupAgent}<`);
    const llm = html.indexOf(`>${en.settings.providers}<`);
    const plugins = html.indexOf(`>${en.settings.plugins}<`);

    expect(client).toBeGreaterThan(-1);
    expect(agent).toBeGreaterThan(client);
    expect(llm).toBeGreaterThan(agent);
    expect(plugins).toBeGreaterThan(agent);
    expect(llm).toBeLessThan(plugins);
    // The fixed pages and the schema sections share one Agent heading.
    expect(html.split(`>${en.settings.configGroupAgent}<`).length - 1).toBe(1);
  });

  it("distinguishes the plugin settings section from the Plugins page", () => {
    seed(["config.settings"]);
    const html = renderToStaticMarkup(<SettingsPage />);
    const pluginSection = buildConfigSections(schema)
      .find((candidate) => candidate.key === "agent.plugins");

    expect(pluginSection).toBeDefined();
    expect(sectionTitle(pluginSection!, "en")).toBe("Plugin settings");
    expect(sectionTitle(pluginSection!, "zh-CN")).toBe("插件设置");
    expect(html).toContain("Plugin settings");
    // Only the dedicated Plugins page keeps the bare name.
    expect(html.split(`>${en.settings.plugins}<`).length - 1).toBe(1);
  });
});

describe("ConfigFieldControl reset", () => {
  it("offers a per-field restore-default action when allowed", () => {
    const html = renderToStaticMarkup(
      <ConfigFieldControl
        spec={spec({ key: "agent.context.max_tokens", title: "Context budget" })}
        field={field({ value: 64000 })}
        value={64000}
        locale="en"
        strings={en}
        scope="new_session"
        onChange={() => undefined}
        onReset={() => undefined}
        resetDisabled={false}
      />,
    );
    expect(html).toContain(en.settings.configResetField);
  });
});

describe("ConfigSettingsSection recovery", () => {
  it("shows a retry for a failed section load instead of a permanent spinner", () => {
    store.dispatch({
      type: "gateway/hello",
      hello: {
        instance_id: "instance-1",
        protocol: 1,
        features: ["config.settings"],
        limits: {},
      },
    });
    store.dispatch({
      type: "configuration/loaded",
      result: {
        revision: "rev-1",
        path: "/tmp/config.yaml",
        exists: true,
        manage: true,
        writable: true,
        schema: {
          version: 1,
          sections: [
            {
              key: "agent.context",
              title: "Context",
              title_i18n: { en: "Context" },
              fields: [spec({ key: "agent.context.max_tokens", title: "Context budget" })],
            },
          ],
        },
        fields: {},
        runtimeScopes: {},
        issues: [],
      },
      sections: ["agent.context"],
      fieldsIncluded: true,
    });
    store.dispatch({
      type: "configuration/sectionError",
      section: "agent.context",
      error: { code: "E_CLIENT", message: "offline" },
    });
    const html = renderToStaticMarkup(<ConfigSettingsSection sectionKey="agent.context" />);
    expect(html).toContain(en.settings.configSectionLoadFailed);
    expect(html).toContain(en.settings.configReload);
    expect(html).not.toContain(en.settings.configLoading);
  });
});

describe("settings sidebar search helpers", () => {
  const contextSection = {
    key: "agent.context",
    title: "Context",
    title_i18n: { en: "Context" },
    fields: [spec({
      key: "agent.context.max_tokens",
      title: "Context budget",
      description: "Maximum context tokens.",
    })],
  };

  it("matches a section by title or one of its field titles/descriptions", () => {
    expect(sectionMatches(contextSection, "context", "en")).toBe(true);
    expect(sectionMatches(contextSection, "budget", "en")).toBe(true);
    expect(sectionMatches(contextSection, "maximum context", "en")).toBe(true);
    expect(sectionMatches(contextSection, "gateway", "en")).toBe(false);
  });

  it("builds generic sections without the dedicated pages or plugin allow", () => {
    const sections = buildConfigSections({
      version: 1,
      sections: [
        contextSection,
        {
          key: "agent.llm", title: "LLM", title_i18n: {}, fields: [],
        },
        {
          key: "agent.profiles", title: "Profiles", title_i18n: {}, fields: [],
        },
        {
          key: "agent.plugins", title: "Plugins", title_i18n: {},
          fields: [
            spec({ key: "agent.plugins.allow", title: "Allowed plugins" }),
            spec({ key: "agent.plugins.dirs", title: "Plugin dirs" }),
          ],
        },
        {
          key: "ui", title: "Interface", title_i18n: {},
          fields: [spec({ key: "ui.theme", title: "Theme" })],
        },
      ],
    });
    expect(sections.map((section) => section.key)).toEqual([
      "agent.context", "agent.plugins",
    ]);
    expect(sections[1].fields.map((field) => field.key)).toEqual([
      "agent.plugins.dirs",
    ]);
  });

  it("filters the active section by query and source", () => {
    const value = field({ value: 32000, source: "file" });
    expect(fieldMatches(contextSection.fields[0], "budget", "all", value, "en"))
      .toBe(true);
    expect(fieldMatches(contextSection.fields[0], "budget", "env", value, "en"))
      .toBe(false);
  });
});
