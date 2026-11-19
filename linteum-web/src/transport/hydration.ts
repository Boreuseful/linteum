import type { ViewFrame } from "../protocol/frames";

/**
 * True when `frame` belongs to the active session while its initial replay is
 * still in flight. Frames without a session id are control frames and are
 * buffered with the replay they accompany.
 */
export function isHydrationFrame(
  session: { id: string; hydrating: boolean } | null,
  frame: { sessionId?: string },
): boolean {
  return Boolean(
    session?.hydrating &&
      (!frame.sessionId || frame.sessionId === session.id),
  );
}

/**
 * Holds replay frames until `session.subscribe` acknowledges the end of the
 * replay.
 *
 * The gateway sends the snapshot and every replayed frame before the command
 * result. Applying them as they arrive makes the transcript grow in chunks and
 * briefly mounts historical approval prompts; collecting them here lets the
 * store commit the finished transcript in a single transition.
 */
export class HydrationBuffer {
  private sessionId: string | null = null;
  private frames: ViewFrame[] = [];

  /** Start (or restart) buffering for a newly issued subscribe command. */
  begin(sessionId: string): void {
    this.sessionId = sessionId;
    this.frames = [];
  }

  /** Append one frame, switching buffers if the session changed. */
  push(sessionId: string, frame: ViewFrame): void {
    if (this.sessionId !== sessionId) {
      this.sessionId = sessionId;
      this.frames = [];
    }
    this.frames.push(frame);
  }

  /**
   * Take the buffered frames for `sessionId` and reset the buffer.
   * A result for another session is ignored (the switch already happened).
   */
  take(sessionId: string): ViewFrame[] {
    if (this.sessionId !== sessionId) return [];
    const frames = this.frames;
    this.sessionId = null;
    this.frames = [];
    return frames;
  }

  /** Drop a failed subscription's frames without handing them to the store. */
  discard(sessionId: string): void {
    if (this.sessionId !== sessionId) return;
    this.sessionId = null;
    this.frames = [];
  }

  clear(): void {
    this.sessionId = null;
    this.frames = [];
  }
}
