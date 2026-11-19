/** Typed command constructors and result shapes (Gateway Protocol v1). */

import {
  asArray,
  asBoolean,
  asNumber,
  asRecord,
  asString,
  asStringArray,
  PROTOCOL_VERSION,
} from "./envelope";

export interface Usage {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  total_tokens?: number;
  cost_usd?: number;
  [key: string]: unknown;
}

export interface SessionInfo {
  session_id: string;
  created_at: number;
  updated_at: number;
  phase: string;
  round: number;
  message_count: number;
  last_seq: number;
  title?: string | null;
  workspace?: string | null;
  metadata?: Record<string, unknown>;
  parent_session_id?: string | null;
  kind?: string;
}

export interface HelloResult {
  protocol: number;
  server: string;
  features: string[];
  limits: Record<string, number>;
  instance_id: string;
}

export interface SessionListResult {
  sessions: SessionInfo[];
  limit: number;
  offset: number;
}

export interface SessionCreateResult {
  session: SessionInfo;
  snapshot?: Record<string, unknown>;
  warnings?: string[];
}

export interface ModeInfo {
  id: string;
  name: string;
  description?: string;
  source?: string;
  builtin?: boolean;
  default?: boolean;
  digest?: string | null;
  from?: string | null;
  refresh?: boolean;
}

export interface ModeListResult {
  modes: ModeInfo[];
  invalid?: unknown[];
}

export interface PluginErrorInfo {
  code: string;
  message: string;
  hint?: string;
}

/**
 * One plugin from `plugin.list`.
 *
 * Capability fields (`tools`/`skills`/`modes`) and extensibility fields
 * (`hooks`/`subscribes`/`publishes`) are always normalized to arrays so the
 * detail card can render them without defensive checks. `config` maps a key to
 * its source (``"default"`` or ``"file"``); secret values are never sent.
 */
export interface PluginSummary {
  id: string;
  name?: string;
  version?: string;
  state?: string;
  enabled?: boolean;
  description?: string;
  trust?: string;
  source?: string;
  tools: string[];
  skills: string[];
  modes: string[];
  digest?: string;
  warnings: string[];
  hooks: string[];
  subscribes: string[];
  publishes: string[];
  config: Record<string, string>;
  secrets: string[];
  error?: PluginErrorInfo;
  [key: string]: unknown;
}

export interface PluginListResult {
  plugins: PluginSummary[];
  invalid?: unknown[];
  manage?: boolean;
  /** File-level `agent.plugins.allow`; absent when the config is unreadable. */
  allow?: string[];
  [key: string]: unknown;
}

/** One entry from `plugin.set_allowed`'s runtime changes. */
export interface PluginChange {
  id: string;
  action: string;
  state: string;
  error?: PluginErrorInfo;
}

export interface ContextInfo {
  session_id: string;
  kind: string;
  phase: string;
  round: number;
  source: string;
  system_blocks_included: boolean;
  digest: string;
  estimated_tokens: number;
  /** One bounded page of provider-facing messages. */
  messages: unknown[];
  /** Total provider-facing messages in the full plan (not just this page). */
  messages_total: number;
  offset: number;
  next_offset: number;
  has_more: boolean;
  /** True when a single message had to be shortened to fit the frame. */
  truncated: boolean;
  tools: string[];
}

export interface ContextResult {
  context: ContextInfo;
}

export interface TaskInfo {
  task_id: string;
  call_id?: string | null;
  name?: string;
  kind?: string;
  status?: string;
  adopted?: boolean;
  started_at?: number;
  started_seq?: number;
  duration_ms?: number;
  finished_at?: number;
  finished_seq?: number;
  [key: string]: unknown;
}

export interface TasksResult {
  session_id: string;
  tasks: TaskInfo[];
}

export interface ChildrenResult {
  parent_session_id: string;
  sessions: SessionInfo[];
  limit: number;
  offset: number;
}

export interface ArtifactReadResult {
  ref: string;
  offset: number;
  size: number;
  chunk_bytes: number;
  eof: boolean;
}

export function helloArgs(name: string, version: string): Record<string, unknown> {
  return { protocol: [PROTOCOL_VERSION], client: { name, version } };
}

export interface SessionListOptions {
  limit?: number;
  offset?: number;
  workspace?: string;
  includeSubagents?: boolean;
}

export function sessionListArgs(options: SessionListOptions = {}): Record<string, unknown> {
  const args: Record<string, unknown> = {
    limit: options.limit ?? 50,
    offset: options.offset ?? 0,
  };
  if (options.workspace) args.workspace = options.workspace;
  if (options.includeSubagents) args.include_subagents = true;
  return args;
}

export interface SessionCreateOptions {
  workspace: string;
  title?: string;
  profile?: string;
  metadata?: Record<string, unknown>;
}

export function sessionCreateArgs(options: SessionCreateOptions): Record<string, unknown> {
  const args: Record<string, unknown> = { workspace: options.workspace };
  if (options.title) args.title = options.title;
  if (options.profile) args.profile = options.profile;
  if (options.metadata) args.metadata = options.metadata;
  return args;
}

export function sessionSubscribeArgs(afterSeq = 0): Record<string, unknown> {
  return { after_seq: afterSeq, mode: "view" };
}

export function userMessageArgs(text: string, clientMessageId: string): Record<string, unknown> {
  return { text, client_message_id: clientMessageId };
}

export function approvalDecideArgs(
  callId: string,
  decision: "grant" | "deny",
  reason?: string,
): Record<string, unknown> {
  const args: Record<string, unknown> = { call_id: callId, decision };
  if (reason) args.reason = reason;
  return args;
}

export function modeSetArgs(modeName: string): Record<string, unknown> {
  return { mode: modeName };
}

export function taskStopArgs(taskId: string, reason?: string): Record<string, unknown> {
  const args: Record<string, unknown> = { task_id: taskId };
  if (reason) args.reason = reason;
  return args;
}

export function artifactReadArgs(
  ref: string,
  offset = 0,
  limit?: number,
): Record<string, unknown> {
  const args: Record<string, unknown> = { ref, offset };
  if (limit) args.limit = limit;
  return args;
}

export function childrenArgs(limit = 50, offset = 0): Record<string, unknown> {
  return { limit, offset };
}

export function historyReadArgs(afterSeq = 0, limit = 100): Record<string, unknown> {
  return { after_seq: afterSeq, limit, mode: "view" };
}

// ---------------------------------------------------------------------------
// Wire → domain parsers for command results
// ---------------------------------------------------------------------------

export function parseSessionInfo(value: unknown): SessionInfo | null {
  const record = asRecord(value);
  const sessionId = asString(record.session_id);
  if (!sessionId) return null;
  return {
    session_id: sessionId,
    created_at: asNumber(record.created_at) ?? 0,
    updated_at: asNumber(record.updated_at) ?? 0,
    phase: asString(record.phase) ?? "idle",
    round: asNumber(record.round) ?? 0,
    message_count: asNumber(record.message_count) ?? 0,
    last_seq: asNumber(record.last_seq) ?? 0,
    title: asString(record.title) ?? null,
    workspace: asString(record.workspace) ?? null,
    metadata: asRecord(record.metadata),
    parent_session_id: asString(record.parent_session_id) ?? null,
    kind: asString(record.kind) ?? "root",
  };
}

export function parseModeInfo(value: unknown): ModeInfo | null {
  const record = asRecord(value);
  const id = asString(record.id);
  const name = asString(record.name);
  if (!id || !name) return null;
  return {
    id,
    name,
    description: asString(record.description),
    source: asString(record.source),
    builtin: asBoolean(record.builtin),
    default: asBoolean(record.default),
  };
}

function parsePluginError(value: unknown): PluginErrorInfo | undefined {
  const record = asRecord(value);
  const code = asString(record.code);
  if (!code) return undefined;
  return {
    code,
    message: asString(record.message) ?? code,
    hint: asString(record.hint),
  };
}

function parsePluginConfig(value: unknown): Record<string, string> {
  const config: Record<string, string> = {};
  for (const [key, raw] of Object.entries(asRecord(value))) {
    if (typeof raw === "string") config[key] = raw;
  }
  return config;
}

export function parsePluginSummary(value: unknown): PluginSummary | null {
  const record = asRecord(value);
  const id = asString(record.id);
  if (!id) return null;
  return {
    id,
    name: asString(record.name),
    version: asString(record.version),
    state: asString(record.state),
    // Older gateways omit `enabled`; `undefined` means "no file-level answer".
    enabled: typeof record.enabled === "boolean" ? record.enabled : undefined,
    description: asString(record.description),
    trust: asString(record.trust),
    source: asString(record.source),
    tools: asStringArray(record.tools),
    skills: asStringArray(record.skills),
    modes: asStringArray(record.modes),
    digest: asString(record.digest),
    warnings: asStringArray(record.warnings),
    hooks: asStringArray(record.hooks),
    subscribes: asStringArray(record.subscribes),
    publishes: asStringArray(record.publishes),
    config: parsePluginConfig(record.config),
    secrets: asStringArray(record.secrets),
    error: parsePluginError(record.error),
  };
}

export function parsePluginChange(value: unknown): PluginChange | null {
  const record = asRecord(value);
  const id = asString(record.id);
  if (!id) return null;
  return {
    id,
    action: asString(record.action) ?? "unchanged",
    state: asString(record.state) ?? "",
    error: parsePluginError(record.error),
  };
}

export function parsePluginChanges(value: unknown): PluginChange[] {
  return asArray(value)
    .map(parsePluginChange)
    .filter((change): change is PluginChange => change !== null);
}

export function pluginSetAllowedArgs(
  ids: readonly string[],
  mode: "deferred" | "apply" = "apply",
): Record<string, unknown> {
  return { ids: [...ids], mode };
}

export function parseContextInfo(value: unknown): ContextInfo | null {
  const record = asRecord(value);
  const sessionId = asString(record.session_id);
  if (!sessionId) return null;
  const messages = asArray(record.messages);
  return {
    session_id: sessionId,
    kind: asString(record.kind) ?? "root",
    phase: asString(record.phase) ?? "idle",
    round: asNumber(record.round) ?? 0,
    source: asString(record.source) ?? "replay",
    system_blocks_included: asBoolean(record.system_blocks_included),
    digest: asString(record.digest) ?? "",
    estimated_tokens: asNumber(record.estimated_tokens) ?? 0,
    messages,
    // Older gateways return the whole plan and no paging fields.
    messages_total: asNumber(record.messages_total) ?? messages.length,
    offset: asNumber(record.offset) ?? 0,
    next_offset: asNumber(record.next_offset) ?? messages.length,
    has_more: asBoolean(record.has_more),
    truncated: asBoolean(record.truncated),
    tools: asStringArray(record.tools),
  };
}

// ---------------------------------------------------------------------------
// Configuration management (web-config design §8.2)
// ---------------------------------------------------------------------------

export interface ConfigFieldSpec {
  key: string;
  type: string;
  default: unknown;
  title: string;
  description: string;
  title_i18n: Record<string, string>;
  description_i18n: Record<string, string>;
  enum: string[];
  minimum: number | null;
  maximum: number | null;
  secret: boolean;
  advanced: boolean;
  apply_scope: string;
  nullable: boolean;
  allow_custom: boolean;
  section: string;
  item_type: string;
  item_section: string;
}

export interface ConfigSchemaSection {
  key: string;
  title: string;
  title_i18n: Record<string, string>;
  fields: ConfigFieldSpec[];
}

export interface ConfigSchema {
  version: number;
  sections: ConfigSchemaSection[];
}

export interface ConfigFieldValue {
  value: unknown;
  source: string;
  secret: boolean;
  set: boolean;
  masked: string | null;
  /** File-level value for non-secret scalars (``${VAR}`` intact). */
  raw_value?: unknown;
  /** Environment variable name when ``source === "env"``. */
  env_var?: string | null;
}

export interface ConfigIssue {
  key: string;
  code: string;
  message: string;
  severity: string;
  hint: string;
}

export interface ConfigGetResult {
  revision: string;
  path: string;
  /** False when config.yaml does not exist yet; the first save creates it. */
  exists: boolean;
  manage: boolean;
  writable: boolean;
  schema: ConfigSchema | null;
  fields: Record<string, ConfigFieldValue>;
  /** Real runtime effect per field (new_session / next_connection / ...). */
  runtimeScopes: Record<string, string>;
  issues: ConfigIssue[];
}

export interface ConfigChange {
  key: string;
  old: unknown;
  new: unknown;
  scope: string;
}

export interface ConfigApply {
  declared: { live: string[]; new_session: string[]; restart: string[] };
  runtime: Record<string, string>;
}

export interface ConfigSetResult {
  revision: string;
  changed: boolean;
  changes: ConfigChange[];
  apply: ConfigApply;
  warnings: ConfigIssue[];
}

export function configGetArgs(
  sections?: readonly string[],
  includeSchema = true,
  includeFields = true,
): Record<string, unknown> {
  // Older gateways only accept ``sections``/``include_schema``.  Omit the
  // default when possible so pre-Phase-3 hosts keep working; only the
  // schema-only bootstrap (includeFields=false) needs the new argument.
  const args: Record<string, unknown> = { include_schema: includeSchema };
  if (sections && sections.length > 0) args.sections = [...sections];
  if (!includeFields) args.include_fields = false;
  return args;
}

export function configSetArgs(
  changes: Record<string, unknown>,
  expectedRevision: string,
  persist = true,
): Record<string, unknown> {
  return {
    changes: { ...changes },
    expected_revision: expectedRevision,
    persist,
  };
}

export function configUnsetArgs(
  keys: readonly string[],
  expectedRevision: string,
): Record<string, unknown> {
  return { keys: [...keys], expected_revision: expectedRevision };
}

export function configProfileSetArgs(
  name: string,
  values: Record<string, unknown>,
  expectedRevision: string,
): Record<string, unknown> {
  return { name, values: { ...values }, expected_revision: expectedRevision };
}

export function configProfileDeleteArgs(
  name: string,
  expectedRevision: string,
  confirmInUse = false,
): Record<string, unknown> {
  return {
    name,
    expected_revision: expectedRevision,
    confirm_in_use: confirmInUse,
  };
}

export function configProfileDuplicateArgs(
  from: string,
  name: string,
  expectedRevision: string,
): Record<string, unknown> {
  return { from, name, expected_revision: expectedRevision };
}

export function configFallbackSetArgs(
  values: Record<string, unknown>,
  expectedRevision: string,
  index?: number,
): Record<string, unknown> {
  const args: Record<string, unknown> = {
    values: { ...values },
    expected_revision: expectedRevision,
  };
  if (index !== undefined) args.index = index;
  return args;
}

export function configFallbackDeleteArgs(
  index: number,
  expectedRevision: string,
): Record<string, unknown> {
  return { index, expected_revision: expectedRevision };
}

export function configFallbackMoveArgs(
  from: number,
  to: number,
  expectedRevision: string,
): Record<string, unknown> {
  return { from, to, expected_revision: expectedRevision };
}

function asStringMap(value: unknown): Record<string, string> {
  const record = asRecord(value);
  const out: Record<string, string> = {};
  for (const [key, raw] of Object.entries(record)) {
    const text = asString(raw);
    if (text !== undefined) out[key] = text;
  }
  return out;
}

function parseConfigFieldSpec(value: unknown): ConfigFieldSpec | null {
  const record = asRecord(value);
  const key = asString(record.key);
  if (!key) return null;
  return {
    key,
    type: asString(record.type) ?? "str",
    default: record.default,
    title: asString(record.title) ?? key,
    description: asString(record.description) ?? "",
    title_i18n: asStringMap(record.title_i18n),
    description_i18n: asStringMap(record.description_i18n),
    enum: asStringArray(record.enum),
    minimum: asNumber(record.minimum) ?? null,
    maximum: asNumber(record.maximum) ?? null,
    secret: asBoolean(record.secret),
    advanced: asBoolean(record.advanced),
    apply_scope: asString(record.apply_scope) ?? "new_session",
    nullable: asBoolean(record.nullable),
    allow_custom: asBoolean(record.allow_custom),
    section: asString(record.section) ?? "",
    item_type: asString(record.item_type) ?? "",
    item_section: asString(record.item_section) ?? "",
  };
}

function parseConfigSchema(value: unknown): ConfigSchema | null {
  const record = asRecord(value);
  if (!record || typeof record !== "object") return null;
  const sections = asArray(record.sections)
    .map((entry): ConfigSchemaSection | null => {
      const section = asRecord(entry);
      const key = asString(section.key);
      if (!key) return null;
      return {
        key,
        title: asString(section.title) ?? key,
        title_i18n: asStringMap(section.title_i18n),
        fields: asArray(section.fields)
          .map(parseConfigFieldSpec)
          .filter((field): field is ConfigFieldSpec => field !== null),
      };
    })
    .filter((section): section is ConfigSchemaSection => section !== null);
  return { version: asNumber(record.version) ?? 1, sections };
}

function parseConfigFieldValue(value: unknown): ConfigFieldValue {
  const record = asRecord(value);
  return {
    value: record.value,
    source: asString(record.source) ?? "default",
    secret: asBoolean(record.secret),
    set: asBoolean(record.set),
    masked: asString(record.masked) ?? null,
    raw_value: record.raw_value,
    env_var: asString(record.env_var) ?? null,
  };
}

function parseConfigIssue(value: unknown): ConfigIssue | null {
  const record = asRecord(value);
  const code = asString(record.code);
  if (!code) return null;
  return {
    key: asString(record.key) ?? "",
    code,
    message: asString(record.message) ?? code,
    severity: asString(record.severity) ?? "error",
    hint: asString(record.hint) ?? "",
  };
}

function parseConfigChange(value: unknown): ConfigChange | null {
  const record = asRecord(value);
  const key = asString(record.key);
  if (!key) return null;
  return {
    key,
    old: record.old,
    new: record.new,
    scope: asString(record.scope) ?? "new_session",
  };
}

function parseRuntimeScopes(value: unknown): Record<string, string> {
  const record = asRecord(value);
  const scopes: Record<string, string> = {};
  for (const [key, raw] of Object.entries(record)) {
    const scope = asString(raw);
    if (scope) scopes[key] = scope;
  }
  return scopes;
}

export function parseConfigGetResult(value: unknown): ConfigGetResult | null {
  const record = asRecord(value);
  const revision = asString(record.revision);
  if (revision === undefined) return null;
  const fields: Record<string, ConfigFieldValue> = {};
  for (const [key, raw] of Object.entries(asRecord(record.fields))) {
    fields[key] = parseConfigFieldValue(raw);
  }
  return {
    revision,
    path: asString(record.path) ?? "",
    exists: asBoolean(record.exists),
    manage: asBoolean(record.manage),
    writable: asBoolean(record.writable),
    schema: record.schema == null ? null : parseConfigSchema(record.schema),
    fields,
    runtimeScopes: parseRuntimeScopes(record.runtime_scopes),
    issues: asArray(record.issues)
      .map(parseConfigIssue)
      .filter((issue): issue is ConfigIssue => issue !== null),
  };
}

export function parseConfigSetResult(value: unknown): ConfigSetResult | null {
  const record = asRecord(value);
  const revision = asString(record.revision);
  if (revision === undefined) return null;
  const apply = asRecord(record.apply);
  const declared = asRecord(apply.declared);
  return {
    revision,
    changed: asBoolean(record.changed),
    changes: asArray(record.changes)
      .map(parseConfigChange)
      .filter((change): change is ConfigChange => change !== null),
    apply: {
      declared: {
        live: asStringArray(declared.live),
        new_session: asStringArray(declared.new_session),
        restart: asStringArray(declared.restart),
      },
      runtime: parseRuntimeScopes(apply.runtime),
    },
    warnings: asArray(record.warnings)
      .map(parseConfigIssue)
      .filter((issue): issue is ConfigIssue => issue !== null),
  };
}
