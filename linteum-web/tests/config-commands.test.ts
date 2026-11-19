import { describe, expect, it } from "vitest";
import {
  configFallbackDeleteArgs,
  configFallbackMoveArgs,
  configFallbackSetArgs,
  configGetArgs,
  configProfileDeleteArgs,
  configProfileDuplicateArgs,
  configProfileSetArgs,
  configSetArgs,
  configUnsetArgs,
  parseConfigGetResult,
  parseConfigSetResult,
} from "../src/protocol/commands";

const getPayload = {
  revision: "rev-1",
  path: "/home/u/.linteum/config.yaml",
  exists: true,
  manage: true,
  writable: true,
  schema: {
    version: 1,
    sections: [
      {
        key: "agent.llm",
        title: "模型",
        fields: [
          {
            key: "agent.llm.model",
            type: "str",
            default: "gpt-4o-mini",
            title: "Model",
            description: "",
            enum: [],
            minimum: null,
            maximum: null,
            secret: false,
            advanced: false,
            apply_scope: "new_session",
            nullable: false,
            allow_custom: false,
            section: "agent.llm",
            item_type: "",
            item_section: "",
          },
        ],
      },
    ],
  },
  fields: {
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
  },
  runtime_scopes: { "agent.llm.model": "new_session" },
  issues: [],
};

describe("config commands", () => {
  it("builds gateway-wide config arguments", () => {
    expect(configGetArgs(["agent.llm"])).toEqual({
      sections: ["agent.llm"],
      include_schema: true,
    });
    expect(configSetArgs({ "agent.llm.model": "x" }, "rev-1")).toEqual({
      changes: { "agent.llm.model": "x" },
      expected_revision: "rev-1",
      persist: true,
    });
    expect(configUnsetArgs(["agent.llm.base_url"], "rev-1")).toEqual({
      keys: ["agent.llm.base_url"],
      expected_revision: "rev-1",
    });
  });

  it("parses config.get and keeps secrets masked", () => {
    const parsed = parseConfigGetResult(getPayload);
    expect(parsed?.runtimeScopes["agent.llm.model"]).toBe("new_session");
    expect(parsed?.fields["agent.llm.api_key"].value).toBeNull();
    expect(parsed?.fields["agent.llm.api_key"].masked).toBe("sk-…abcd");
    expect(parsed?.exists).toBe(true);
  });

  it("parses config.set changes and apply scopes", () => {
    const parsed = parseConfigSetResult({
      revision: "rev-2",
      changed: true,
      changes: [
        { key: "agent.llm.model", old: "a", new: "b", scope: "new_session" },
      ],
      apply: {
        declared: { live: [], new_session: ["agent.llm.model"], restart: [] },
        runtime: { "agent.llm.model": "new_session" },
      },
      warnings: [],
    });
    expect(parsed?.changed).toBe(true);
    expect(parsed?.changes[0].key).toBe("agent.llm.model");
    expect(parsed?.apply.runtime["agent.llm.model"]).toBe("new_session");
  });

  it("returns null for unusable payloads", () => {
    expect(parseConfigGetResult(null)).toBeNull();
    expect(parseConfigGetResult({})).toBeNull();
    expect(parseConfigSetResult({ revision: "rev" })).not.toBeNull();
  });

  it("builds profile and fallback commands", () => {
    expect(configProfileSetArgs("fast", { provider: "deepseek" }, "rev")).toEqual({
      name: "fast", values: { provider: "deepseek" }, expected_revision: "rev",
    });
    expect(configProfileDeleteArgs("fast", "rev")).toEqual({
      name: "fast", expected_revision: "rev", confirm_in_use: false,
    });
    expect(configProfileDeleteArgs("fast", "rev", true)).toEqual({
      name: "fast", expected_revision: "rev", confirm_in_use: true,
    });
    expect(configProfileDuplicateArgs("default", "fast", "rev")).toEqual({
      from: "default", name: "fast", expected_revision: "rev",
    });
    expect(configFallbackSetArgs({ provider: "openai" }, "rev", 0)).toEqual({
      values: { provider: "openai" }, expected_revision: "rev", index: 0,
    });
    expect(configFallbackDeleteArgs(1, "rev")).toEqual({
      index: 1, expected_revision: "rev",
    });
    expect(configFallbackMoveArgs(0, 1, "rev")).toEqual({
      from: 0, to: 1, expected_revision: "rev",
    });
  });
});

describe("phase 3 config protocol", () => {
  it("passes include_fields through configGetArgs", () => {
    expect(configGetArgs(["agent.context"], true, false)).toEqual({
      sections: ["agent.context"],
      include_schema: true,
      include_fields: false,
    });
    expect(configGetArgs(undefined, true, true)).toEqual({
      include_schema: true,
    });
  });

  it("parses schema i18n, env_var and raw_value", () => {
    const payload: any = {
      ...getPayload,
      schema: {
        version: 1,
        sections: [
          {
            key: "agent.context",
            title: "上下文",
            title_i18n: { en: "Context" },
            fields: [
              {
                ...getPayload.schema.sections[0].fields[0],
                key: "agent.context.max_tokens",
                title_i18n: { en: "Context budget" },
                description_i18n: { en: "Max tokens." },
              },
            ],
          },
        ],
      },
      fields: {
        "agent.context.max_tokens": {
          value: 32000,
          raw_value: 32000,
          source: "file",
          secret: false,
          set: true,
          masked: null,
        },
        "logging.level": {
          value: "debug",
          source: "env",
          secret: false,
          set: true,
          masked: null,
          env_var: "LINTEUM_LOGGING__LEVEL",
        },
      },
    };
    const parsed = parseConfigGetResult(payload);
    expect(parsed?.schema?.sections[0].title_i18n).toEqual({ en: "Context" });
    expect(parsed?.schema?.sections[0].fields[0].title_i18n).toEqual({
      en: "Context budget",
    });
    expect(parsed?.schema?.sections[0].fields[0].description_i18n).toEqual({
      en: "Max tokens.",
    });
    expect(parsed?.fields["agent.context.max_tokens"].raw_value).toBe(32000);
    expect(parsed?.fields["logging.level"].env_var).toBe(
      "LINTEUM_LOGGING__LEVEL",
    );
  });
});
