/** Exponential backoff with jitter, capped at `maxMs`. */
export class ReconnectPolicy {
  private attempt = 0;

  constructor(
    private readonly baseMs = 500,
    private readonly maxMs = 8_000,
  ) {}

  nextDelay(): number {
    const exponential = Math.min(this.maxMs, this.baseMs * 2 ** this.attempt);
    this.attempt += 1;
    const jittered = exponential * (0.85 + Math.random() * 0.3);
    return Math.max(this.baseMs, Math.min(this.maxMs, Math.round(jittered)));
  }

  reset(): void {
    this.attempt = 0;
  }

  get attempts(): number {
    return this.attempt;
  }
}
