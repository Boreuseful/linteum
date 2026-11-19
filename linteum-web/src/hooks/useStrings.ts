import { en, zhCN, type Strings } from "../strings";
import { useAppState } from "./useStore";

/** The active copy catalogue. Components re-render when the locale changes. */
export function useStrings(): Strings {
  const state = useAppState();
  return state.ui.locale === "zh-CN" ? zhCN : en;
}
