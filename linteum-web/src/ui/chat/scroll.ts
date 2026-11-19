/** Distance (px) from the bottom that still counts as "pinned". */
export const BOTTOM_THRESHOLD = 80;
/** A real detach moves the viewport up by at least this much. */
export const UPWARD_EPSILON = 1;

export interface ScrollSample {
  top: number;
  /** Scroll container height (scrollHeight). */
  height: number;
  /** Visible height (clientHeight). */
  viewport: number;
  previousTop: number;
}

export type ScrollDecision = "bottom" | "detach" | "ignore";

/**
 * Classify one scroll event without knowing who caused it.
 *
 * Programmatic sticky-scroll only ever moves the viewport **down**; content
 * that grew between our scroll and the event may leave a gap larger than the
 * threshold. Treating that as a detach is what made the transcript lose its
 * auto-follow while the user was not touching it. Only an upward move can
 * genuinely mean "the user scrolled away".
 */
export function evaluateScroll(sample: ScrollSample): ScrollDecision {
  const distance = sample.height - sample.top - sample.viewport;
  if (distance <= BOTTOM_THRESHOLD) return "bottom";
  if (sample.top < sample.previousTop - UPWARD_EPSILON) return "detach";
  return "ignore";
}
