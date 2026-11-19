import { useSyncExternalStore } from "react";
import { store } from "../state/instance";
import type { AppState } from "../state/types";

export function useAppState(): AppState {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

export { store };
