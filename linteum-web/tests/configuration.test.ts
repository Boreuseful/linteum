import { describe, expect, it } from "vitest";
import {
  PROFILE_FIELDS,
  collectConfigChanges,
  collectFallbackChanges,
  collectProfileChanges,
  configEditorDraft,
  configErrorKey,
  configKeySection,
  configReadBlocked,
  configReloadSections,
  INITIAL_CONFIGURATION,
  needsProfileFields,
  validateConfigChanges,
  fallbackValues,
  profileCatalog,
  profileNameError,
  profileValues,
  type SecretEdit,
} from "../src/state/configuration";
import type { ConfigFieldSpec, ConfigFieldValue } from "../src/protocol/commands";

const fields: Record<string, ConfigFieldValue> = {
  "agent.llm.model": {
    value: "gpt-4o-mini",
    source: "default",
    secret: false,
    set: true,
    masked: null,
  },
  "agent.llm.api_key": {
    value: null,
    source: "file",
    secret: true,
    set: true,
    masked: "sk-…abcd",
  },
};

describe("configuration draft diff", () => {
  it("keeps only dirty non-secret fields", () => {
    expect(
      collectConfigChanges(
        { "agent.llm.model": "deepseek-chat", "agent.llm.provider": "openai" },
        fields,
        {},
      ),
    ).toEqual({ "agent.llm.model": "deepseek-chat" });
  });

  it("omits unchanged and empty secret edits", () => {
    const secrets: Record<string, SecretEdit> = {
      "agent.llm.api_key": { mode: "unchanged" },
    };
    expect(collectConfigChanges({}, fields, secrets)).toEqual({});
  });

  it("sends replace and clear secret edits", () => {
    const secrets: Record<string, SecretEdit> = {
      "agent.llm.api_key": { mode: "replace", value: "${DEEPSEEK_API_KEY}" },
    };
    expect(collectConfigChanges({}, fields, secrets)).toEqual({
      "agent.llm.api_key": "${DEEPSEEK_API_KEY}",
    });
    secrets["agent.llm.api_key"] = { mode: "clear" };
    expect(collectConfigChanges({}, fields, secrets)).toEqual({
      "agent.llm.api_key": null,
    });
  });

  it("omits an empty secret replacement", () => {
    expect(
      collectConfigChanges({}, fields, {
        "agent.llm.api_key": { mode: "replace", value: "" },
      }),
    ).toEqual({});
  });

  it("extracts the failing config key", () => {
    expect(
      configErrorKey({
        code: "E_CONFIG_TYPE",
        message: "invalid",
        context: { key: "agent.llm.max_tokens" },
      }),
    ).toBe("agent.llm.max_tokens");
    expect(
      configErrorKey({ code: "E_CONFIG_CONFLICT", message: "conflict" }),
    ).toBeNull();
    expect(configErrorKey(null)).toBeNull();
  });

  it("blocks editing for parse and io errors", () => {
    expect(configReadBlocked({ code: "E_CONFIG_PARSE", message: "bad" })).toBe(true);
    expect(configReadBlocked({ code: "E_CONFIG_IO", message: "io" })).toBe(true);
    expect(configReadBlocked({ code: "E_CONFIG_TYPE", message: "type" })).toBe(true);
    expect(configReadBlocked(null)).toBe(false);
  });
});

const profileFields: Record<string, ConfigFieldValue> = {
  "agent.default_profile": { value: "fast", source: "file", secret: false, set: true, masked: null },
  "agent.profiles": {
    value: {
      fast: { provider: "deepseek", model: "deepseek-chat", api_key: "sk-…abcd", temperature: 0.2 },
      claude: { provider: "anthropic", model: "claude-sonnet-5-5" },
    },
    source: "file", secret: false, set: true, masked: null,
  },
  "agent.fallbacks": {
    value: [{ provider: "openai", model: "gpt-4o-mini", api_key: "sk-…fb" }],
    source: "file", secret: false, set: true, masked: null,
  },
  "agent.llm.provider": { value: "openai", source: "file", secret: false, set: true, masked: null },
  "agent.llm.model": { value: "gpt-4o-mini", source: "file", secret: false, set: true, masked: null },
};

describe("profile and fallback helpers", () => {
  it("catalogs profiles and default", () => {
    expect(PROFILE_FIELDS).toContain("api_key");
    expect(profileCatalog(profileFields)).toEqual({
      defaultProfile: "fast", names: ["claude", "fast"],
    });
  });

  it("reads default and named profile values", () => {
    expect(profileValues(profileFields, "default")).toMatchObject({
      provider: "openai", model: "gpt-4o-mini",
    });
    expect(profileValues(profileFields, "fast")).toMatchObject({
      provider: "deepseek", model: "deepseek-chat", temperature: 0.2,
    });
    expect(fallbackValues(profileFields, 0)).toMatchObject({
      provider: "openai", model: "gpt-4o-mini",
    });
  });

  it("sends only dirty profile fields and secret edits", () => {
    expect(collectProfileChanges(
      { model: "deepseek-chat", temperature: 0.2 },
      profileValues(profileFields, "fast"),
      {},
    )).toEqual({});
    expect(collectProfileChanges(
      { model: "deepseek-reasoner" },
      profileValues(profileFields, "fast"),
      { api_key: { mode: "replace", value: "${DEEPSEEK_API_KEY}" } },
    )).toEqual({
      model: "deepseek-reasoner", api_key: "${DEEPSEEK_API_KEY}",
    });
    expect(collectFallbackChanges(
      {}, fallbackValues(profileFields, 0),
      { api_key: { mode: "clear" } },
    )).toEqual({ api_key: null });
  });

  it("validates profile names", () => {
    expect(profileNameError("fast", ["claude"], )).toBeNull();
    expect(profileNameError("default", [])).toBe("reserved");
    expect(profileNameError("token", [])).toBe("reserved");
    expect(profileNameError("a b", [])).toBe("invalid");
    expect(profileNameError("fast", ["fast"])).toBe("exists");
  });
});

describe("phase 3 configuration helpers", () => {
  it("compares drafts against raw_value when present", () => {
    const rawFields: Record<string, ConfigFieldValue> = {
      "logging.path": {
        value: "/var/log/linteum.log",
        raw_value: "${LOG_DIR}/linteum.log",
        source: "file",
        secret: false,
        set: true,
        masked: null,
      },
    };
    expect(collectConfigChanges(
      { "logging.path": "${LOG_DIR}/linteum.log" },
      rawFields,
      {},
    )).toEqual({});
    expect(collectConfigChanges(
      { "logging.path": "/tmp/other.log" },
      rawFields,
      {},
    )).toEqual({ "logging.path": "/tmp/other.log" });
  });

  it("maps keys to their schema section", () => {
    expect(configKeySection("agent.llm.model", null)).toBe("agent.llm");
    expect(configKeySection("agent.profiles.fast.model", null)).toBe(
      "agent.profiles",
    );
    expect(configKeySection("agent.fallbacks.0.model", null)).toBe(
      "agent.profiles",
    );
    expect(configKeySection("agent.context.max_tokens", null)).toBe(
      "agent.context",
    );
  });

  it("blocks editing for unparseable/unknown files too", () => {
    expect(configReadBlocked({ code: "E_CONFIG_TYPE", message: "type" })).toBe(true);
    expect(configReadBlocked({ code: "E_CONFIG_UNKNOWN", message: "key" })).toBe(true);
    expect(configReadBlocked({ code: "E_CONFIG_ENUM", message: "enum" })).toBe(false);
  });
});

describe("phase 3 client-side validation", () => {
  const specs: ConfigFieldSpec[] = [
    {
      key: "agent.context.max_tokens", type: "int", default: 32000,
      title: "Max", description: "", title_i18n: {}, description_i18n: {},
      enum: [], minimum: 1, maximum: 100000, secret: false, advanced: false,
      apply_scope: "new_session", nullable: false, allow_custom: false,
      section: "agent.context", item_type: "", item_section: "",
    },
    {
      key: "agent.context.policy", type: "enum", default: "compacting",
      title: "Policy", description: "", title_i18n: {}, description_i18n: {},
      enum: ["passthrough", "compacting"], minimum: null, maximum: null,
      secret: false, advanced: false, apply_scope: "new_session",
      nullable: false, allow_custom: false, section: "agent.context",
      item_type: "", item_section: "",
    },
    {
      key: "agent.tools.auto_approve", type: "list", default: [],
      title: "Tools", description: "", title_i18n: {}, description_i18n: {},
      enum: [], minimum: null, maximum: null, secret: false, advanced: false,
      apply_scope: "new_session", nullable: false, allow_custom: false,
      section: "agent.tools", item_type: "str", item_section: "",
    },
  ];

  it("reports required, range, enum and list-item errors", () => {
    expect(validateConfigChanges(specs, {
      "agent.context.max_tokens": undefined,
    })).toEqual({ "agent.context.max_tokens": "configFieldRequired" });
    expect(validateConfigChanges(specs, {
      "agent.context.max_tokens": 0,
    })).toEqual({ "agent.context.max_tokens": "configFieldRange" });
    expect(validateConfigChanges(specs, {
      "agent.context.policy": "bogus",
    })).toEqual({ "agent.context.policy": "configFieldEnum" });
    expect(validateConfigChanges(specs, {
      "agent.tools.auto_approve": ["read_file", 1],
    })).toEqual({ "agent.tools.auto_approve": "configFieldListType" });
  });

  it("accepts valid values", () => {
    expect(validateConfigChanges(specs, {
      "agent.context.max_tokens": 64000,
      "agent.context.policy": "passthrough",
      "agent.tools.auto_approve": ["read_file"],
    })).toEqual({});
  });
});

describe("phase 3 profile bootstrap", () => {
  it("detects when the profile catalogue still needs loading", () => {
    expect(needsProfileFields(INITIAL_CONFIGURATION)).toBe(true);
    expect(needsProfileFields({
      ...INITIAL_CONFIGURATION,
      fields: {
        "agent.profiles": {
          value: {}, source: "file", secret: false, set: true, masked: null,
        },
        "agent.default_profile": {
          value: "default", source: "file", secret: false, set: true, masked: null,
        },
      },
    })).toBe(false);
  });
});

describe("phase 3 draft edge cases", () => {
  it("drops undefined intermediate values instead of sending them", () => {
    expect(collectConfigChanges(
      { "agent.context.max_tokens": undefined },
      {
        "agent.context.max_tokens": {
          value: 32000, source: "file", secret: false, set: true, masked: null,
        },
      },
      {},
    )).toEqual({});
  });
});

describe("phase 4 editor draft reporting", () => {
  const editorFields: Record<string, ConfigFieldValue> = {
    "agent.llm.model": {
      value: "gpt-4o-mini", source: "file", secret: false, set: true, masked: null,
    },
    "agent.profiles": {
      value: { fast: { provider: "openai", model: "fast-model", api_key: "x" } },
      source: "file", secret: false, set: true, masked: null,
    },
    "agent.fallbacks": {
      value: [{ provider: "openai", model: "fallback-model", api_key: "y" }],
      source: "file", secret: false, set: true, masked: null,
    },
  };

  it("maps the default profile editor to agent.llm", () => {
    expect(configEditorDraft(
      { kind: "profile", name: "default" },
      editorFields,
      { model: "deepseek-chat" },
      {},
    )).toEqual({ section: "agent.llm", dirty: true });
  });

  it("maps named profiles and fallbacks to agent.profiles", () => {
    expect(configEditorDraft(
      { kind: "profile", name: "fast" },
      editorFields,
      { model: "changed" },
      {},
    )).toEqual({ section: "agent.profiles", dirty: true });
    expect(configEditorDraft(
      { kind: "fallback", index: 0 },
      editorFields,
      { model: "changed" },
      {},
    )).toEqual({ section: "agent.profiles", dirty: true });
  });

  it("reports clean when the draft matches the gateway values", () => {
    expect(configEditorDraft(
      { kind: "profile", name: "default" },
      editorFields,
      {},
      {},
    )).toEqual({ section: "agent.llm", dirty: false });
  });

  it("does not report drafts for dialogs without config values", () => {
    expect(configEditorDraft(null, editorFields, { model: "x" }, {})).toBeNull();
    expect(configEditorDraft(
      { kind: "duplicate", from: "fast" }, editorFields, { model: "x" }, {},
    )).toBeNull();
  });
});

describe("phase 4 reload target selection", () => {
  it("refreshes every loaded section, not just the banner's section", () => {
    const configuration = {
      ...INITIAL_CONFIGURATION,
      loadedSections: {
        "agent.llm": { loading: false, error: null, loadedAtRevision: "rev-1" },
        logging: { loading: false, error: null, loadedAtRevision: "rev-1" },
      },
    };
    expect(configReloadSections(configuration, ["agent.llm"])).toEqual([
      "agent.llm", "logging",
    ]);
  });

  it("falls back to the provided sections when nothing is loaded", () => {
    expect(configReloadSections(
      { ...INITIAL_CONFIGURATION, loadedSections: {} }, ["agent.llm"],
    )).toEqual(["agent.llm"]);
  });
});
