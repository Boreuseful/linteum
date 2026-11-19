import type { Action, AppState, Effect } from "./types";

export type Reducer = (state: AppState, action: Action) => { state: AppState; effects: Effect[] };
export type EffectHandler = (effects: Effect[]) => void;

/**
 * Framework-agnostic store.
 *
 * The reducer is pure; effects describe work the controller performs (send a
 * command, reconnect, persist a preference). React only subscribes through
 * `useSyncExternalStore`.
 */
export class AppStore {
  private state: AppState;
  private readonly listeners = new Set<() => void>();
  private effectHandler: EffectHandler | null = null;

  constructor(
    private readonly reducer: Reducer,
    initial: AppState,
  ) {
    this.state = initial;
  }

  getState = (): AppState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  dispatch = (action: Action): void => {
    const { state, effects } = this.reducer(this.state, action);
    if (state !== this.state) {
      this.state = state;
      for (const listener of this.listeners) listener();
    }
    if (effects.length > 0) {
      this.effectHandler?.(effects);
    }
  };

  setEffectHandler(handler: EffectHandler | null): void {
    this.effectHandler = handler;
  }
}
