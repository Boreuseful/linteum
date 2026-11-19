import {
  CLIENT_NAME,
  CLIENT_VERSION,
  HELLO_TIMEOUT_MS,
  PING_INTERVAL_MS,
  RECONNECT_BASE_MS,
  RECONNECT_MAX_MS,
} from "../config";
import { helloArgs, type HelloResult } from "../protocol/commands";
import {
  asNumber,
  asRecord,
  asString,
  asStringArray,
  decodeEnvelope,
  type FrameEnvelope,
  type ResultEnvelope,
} from "../protocol/envelope";
import { GatewayCommandError, localError } from "../protocol/errors";
import { CommandBroker, type CommandOptions } from "./broker";
import { ReconnectPolicy } from "./reconnect";
import type { Transport, TransportCloseInfo } from "./types";
import { WebSocketTransport } from "./websocket";

export type ClientStatus =
  | "idle"
  | "connecting"
  | "handshaking"
  | "ready"
  | "reconnecting"
  | "closed"
  | "error";

export interface GatewayClientHandlers {
  onStatus: (status: ClientStatus, detail?: { error?: string; retryAt?: number }) => void;
  onHello: (hello: HelloResult, reconnected: boolean) => void;
  onFrame: (frame: FrameEnvelope) => void;
  onUnknown: (raw: string) => void;
}

function parseHello(result: ResultEnvelope): HelloResult {
  const payload = result.result;
  return {
    protocol: asNumber(payload.protocol) ?? 1,
    server: asString(payload.server) ?? "linteum",
    features: asStringArray(payload.features),
    limits: asRecord(payload.limits) as Record<string, number>,
    instance_id: asString(payload.instance_id) ?? "unknown",
  };
}

function messageOf(error: unknown): string {
  if (error instanceof GatewayCommandError) {
    return `${error.errorInfo.code}: ${error.errorInfo.message}`;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * Owns one gateway connection: handshake, heartbeat, reconnect and command
 * correlation. It has no session semantics; frames are forwarded untouched.
 */
export class GatewayClient {
  private readonly broker: CommandBroker;
  private readonly policy = new ReconnectPolicy(RECONNECT_BASE_MS, RECONNECT_MAX_MS);
  private url = "";
  private manual = false;
  private instanceId: string | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private currentStatus: ClientStatus = "idle";
  private helloDone = false;

  constructor(
    private readonly handlers: GatewayClientHandlers,
    private readonly transport: Transport = new WebSocketTransport(),
  ) {
    this.broker = new CommandBroker((raw) => this.transport.send(raw));
    this.transport.setHandlers({
      onOpen: () => {
        void this.handleOpen();
      },
      onMessage: (raw) => this.handleMessage(raw),
      onClose: (info) => this.handleClose(info),
      onError: (message) => this.handleError(message),
    });
  }

  get status(): ClientStatus {
    return this.currentStatus;
  }

  get connected(): boolean {
    return this.currentStatus === "ready";
  }

  connect(url: string): void {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") {
        throw new Error("gateway URL must start with ws:// or wss://");
      }
    } catch (error) {
      this.setStatus("error", { error: messageOf(error) });
      return;
    }
    this.url = url;
    this.manual = false;
    this.instanceId = null;
    this.clearReconnect();
    this.broker.rejectAll("gateway reconnecting");
    this.open();
  }

  private open(): void {
    if (!this.url) return;
    this.setStatus(this.instanceId ? "reconnecting" : "connecting");
    this.helloDone = false;
    this.transport.connect(this.url);
  }

  private setStatus(
    status: ClientStatus,
    detail?: { error?: string; retryAt?: number },
  ): void {
    this.currentStatus = status;
    this.handlers.onStatus(status, detail);
  }

  private async handleOpen(): Promise<void> {
    if (this.manual) return;
    this.setStatus("handshaking");
    try {
      const result = await this.broker.call(
        "hello",
        helloArgs(CLIENT_NAME, CLIENT_VERSION),
        { timeoutMs: HELLO_TIMEOUT_MS },
      );
      const hello = parseHello(result);
      const reconnected = this.instanceId !== null;
      this.instanceId = hello.instance_id;
      this.helloDone = true;
      this.policy.reset();
      this.setStatus("ready");
      this.startPing();
      this.handlers.onHello(hello, reconnected);
    } catch (error) {
      if (this.manual) return;
      this.setStatus("error", { error: messageOf(error) });
      this.transport.close();
    }
  }

  private handleMessage(raw: string): void {
    const envelope = decodeEnvelope(raw);
    if (!envelope) {
      this.handlers.onUnknown(raw);
      return;
    }
    if (envelope.kind === "result") {
      this.broker.handleResult(envelope);
      return;
    }
    this.handlers.onFrame(envelope);
  }

  private handleClose(info: TransportCloseInfo): void {
    this.stopPing();
    this.broker.rejectAll(`gateway connection closed: ${info.reason}`);
    if (this.manual) {
      this.setStatus("closed");
      return;
    }
    const delay = this.policy.nextDelay();
    this.setStatus("reconnecting", { retryAt: Date.now() + delay });
    this.clearReconnect();
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  private handleError(message: string): void {
    // A socket error is followed by a close event; the reconnect policy stays
    // in charge of retries. This merely surfaces the reason early.
    this.setStatus(this.instanceId ? "reconnecting" : "error", { error: message });
  }

  private startPing(): void {
    this.stopPing();
    this.pingTimer = setInterval(() => {
      if (this.currentStatus !== "ready") return;
      void this.broker.call("ping", {}, { timeoutMs: 5_000 }).catch(() => undefined);
    }, PING_INTERVAL_MS);
  }

  private stopPing(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  call(
    name: string,
    args: Record<string, unknown>,
    options: CommandOptions = {},
  ): Promise<ResultEnvelope> {
    if (this.currentStatus !== "ready") {
      return Promise.reject(
        new GatewayCommandError(
          localError("E_CONNECTION_CLOSED", "gateway is not connected"),
        ),
      );
    }
    return this.broker.call(name, args, options);
  }

  close(): void {
    this.manual = true;
    this.clearReconnect();
    this.stopPing();
    this.broker.rejectAll("gateway connection closed");
    this.transport.close();
    this.setStatus("closed");
  }
}
