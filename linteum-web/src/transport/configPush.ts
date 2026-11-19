/**
 * Coalesce `config.changed` bursts (web-config-phase4 §6.5.2).
 *
 * One save can emit several pushes (a batch write, or a save immediately
 * followed by its own write). Waiting one 100 ms window keeps the client to a
 * single refresh and a single banner transition; the latest revision and the
 * union of changed keys survive.
 */

import type { ConfigChangedKey } from "../protocol/frames";

export interface ConfigPush {
  revision: string;
  keys: ConfigChangedKey[];
}

/** Latest revision wins; keys are de-duplicated by key (last scope wins). */
export function mergeConfigPushes(
  current: ConfigPush | null,
  next: ConfigPush,
): ConfigPush {
  if (!current) return { revision: next.revision, keys: [...next.keys] };
  const byKey = new Map<string, ConfigChangedKey>();
  for (const item of current.keys) byKey.set(item.key, item);
  for (const item of next.keys) byKey.set(item.key, item);
  return { revision: next.revision, keys: [...byKey.values()] };
}

export class ConfigPushCoalescer {
  private pending: ConfigPush | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly emit: (push: ConfigPush) => void,
    private readonly delayMs = 100,
  ) {}

  /** Buffer one push; the first push in a window starts the timer. */
  push(update: ConfigPush): void {
    this.pending = mergeConfigPushes(this.pending, update);
    if (this.timer !== null) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      const pending = this.pending;
      this.pending = null;
      if (pending) this.emit(pending);
    }, this.delayMs);
  }

  /** Drop a pending window (disconnect / new handshake). */
  clear(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.pending = null;
  }
}
