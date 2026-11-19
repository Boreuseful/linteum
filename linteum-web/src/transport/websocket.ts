import type {
  Transport,
  TransportCloseInfo,
  TransportHandlers,
  TransportStatus,
} from "./types";

/** Browser WebSocket transport. Unknown/binary messages are ignored. */
export class WebSocketTransport implements Transport {
  private socket: WebSocket | null = null;
  private handlers: TransportHandlers = {};
  private currentStatus: TransportStatus = "closed";

  get status(): TransportStatus {
    return this.currentStatus;
  }

  setHandlers(handlers: TransportHandlers): void {
    this.handlers = handlers;
  }

  connect(url: string): void {
    this.close();
    this.currentStatus = "connecting";
    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.currentStatus = "closed";
      this.handlers.onError?.(message);
      this.handlers.onClose?.({ code: 0, reason: message });
      return;
    }
    this.socket = socket;

    socket.addEventListener("open", () => {
      if (this.socket !== socket) return;
      this.currentStatus = "open";
      this.handlers.onOpen?.();
    });

    socket.addEventListener("message", (event: MessageEvent) => {
      if (this.socket !== socket) return;
      if (typeof event.data === "string") {
        this.handlers.onMessage?.(event.data);
      }
    });

    socket.addEventListener("close", (event: CloseEvent) => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.currentStatus = "closed";
      const info: TransportCloseInfo = {
        code: event.code,
        reason: event.reason || `closed (${event.code})`,
      };
      this.handlers.onClose?.(info);
    });

    socket.addEventListener("error", () => {
      if (this.socket !== socket) return;
      this.handlers.onError?.("WebSocket transport error");
    });
  }

  send(raw: string): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error("transport is not open");
    }
    this.socket.send(raw);
  }

  close(): void {
    const socket = this.socket;
    this.socket = null;
    this.currentStatus = "closed";
    if (socket) {
      socket.close();
    }
  }
}
