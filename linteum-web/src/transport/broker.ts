import { encodeCommand, makeCommand, type ResultEnvelope } from "../protocol/envelope";
import { GatewayCommandError, localError } from "../protocol/errors";

export interface CommandOptions {
  sessionId?: string;
  traceId?: string;
  timeoutMs?: number;
}

interface PendingCommand {
  name: string;
  resolve: (result: ResultEnvelope) => void;
  reject: (error: unknown) => void;
  timer: ReturnType<typeof setTimeout>;
}

/**
 * Correlates command ids with `reply_to` results. Frames bypass the broker and
 * flow straight to the renderer, so a frame may legitimately arrive before its
 * subscribe result.
 */
export class CommandBroker {
  private readonly pending = new Map<string, PendingCommand>();

  constructor(
    private readonly sendRaw: (raw: string) => void,
    private readonly defaultTimeoutMs = 30_000,
  ) {}

  call(
    name: string,
    args: Record<string, unknown>,
    options: CommandOptions = {},
  ): Promise<ResultEnvelope> {
    const command = makeCommand(name, args, options.sessionId, options.traceId);
    const raw = encodeCommand(command);
    const timeoutMs = options.timeoutMs ?? this.defaultTimeoutMs;

    return new Promise<ResultEnvelope>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(command.id);
        reject(
          new GatewayCommandError(
            localError("E_TIMEOUT", `command ${name} timed out after ${timeoutMs}ms`),
          ),
        );
      }, timeoutMs);

      this.pending.set(command.id, { name, resolve, reject, timer });

      try {
        this.sendRaw(raw);
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(command.id);
        reject(error);
      }
    });
  }

  handleResult(result: ResultEnvelope): void {
    const pending = this.pending.get(result.reply_to);
    if (!pending) return;
    this.pending.delete(result.reply_to);
    clearTimeout(pending.timer);
    if (result.ok) {
      pending.resolve(result);
    } else {
      pending.reject(
        new GatewayCommandError(
          result.error ?? localError("E_INTERNAL", `command ${pending.name} failed`),
        ),
      );
    }
  }

  rejectAll(reason: string): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      this.pending.delete(id);
      pending.reject(
        new GatewayCommandError(localError("E_CONNECTION_CLOSED", reason)),
      );
    }
  }

  get size(): number {
    return this.pending.size;
  }
}
