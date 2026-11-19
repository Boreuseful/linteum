import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it } from "vitest";
import type { ConfigFieldSpec, ConfigFieldValue } from "../src/protocol/commands";
import { store } from "../src/state/instance";
import { en } from "../src/strings";
import { ConfigSettingsSection } from "../src/ui/settings/ConfigSettingsSection";

function spec(overrides: Partial<ConfigFieldSpec> = {}): ConfigFieldSpec {
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

function field(overrides: Partial<ConfigFieldValue> = {}): ConfigFieldValue {
  return {
    value: 32000,
    source: "file",
    secret: false,
    set: true,
    masked: null,
    ...overrides,
  };
}

function seed(revision = "rev-1"): void {
  store.dispatch({
    type: "gateway/hello",
    hello: {
      instance_id: "instance-1",
      protocol: 1,
      features: ["config.settings", "config.push"],
      limits: {},
    },
  });
  store.dispatch({
    type: "configuration/loaded",
    result: {
      revision,
      path: "/tmp/config.yaml",
      exists: true,
      manage: true,
      writable: true,
      schema: {
        version: 1,
        sections: [{
          key: "agent.context",
          title: "Context",
          title_i18n: { en: "Context" },
          fields: [spec()],
        }],
      },
      fields: { "agent.context.max_tokens": field() },
      runtimeScopes: { "agent.context.max_tokens": "new_session" },
      issues: [],
    },
    sections: ["agent.context"],
    fieldsIncluded: true,
    remoteEpochAtRequest: 0,
  });
}

beforeEach(() => {
  store.dispatch({ type: "ui/locale", locale: "en" });
  store.dispatch({ type: "configuration/reset" });
});

describe("config sync banner (phase 4)", () => {
  it("warns about a remote change with a draft and disables Apply", () => {
    seed();
    store.dispatch({
      type: "configuration/draft",
      section: "agent.context",
      dirty: true,
    });
    store.dispatch({
      type: "configuration/changed",
      revision: "rev-2",
      keys: [{ key: "agent.context.max_tokens", scope: "new_session" }],
    });

    const html = renderToStaticMarkup(
      <ConfigSettingsSection sectionKey="agent.context" />,
    );
    expect(html).toContain(en.settings.configRemoteChanged);
    expect(html).toContain("rev-2");
    expect(html).toContain(en.settings.configReloadDiscard);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Apply<\/button>/);
  });

  it("does not show the banner for its own revision", () => {
    seed();
    store.dispatch({
      type: "configuration/changed",
      revision: "rev-1",
      keys: [{ key: "agent.context.max_tokens", scope: "new_session" }],
    });

    const html = renderToStaticMarkup(
      <ConfigSettingsSection sectionKey="agent.context" />,
    );
    expect(html).not.toContain(en.settings.configRemoteChanged);
  });
});
