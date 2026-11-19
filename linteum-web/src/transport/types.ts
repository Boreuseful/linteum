export interface TransportCloseInfo {
  code: number;
  reason: string;
}

export interface TransportHandlers {
  onOpen?: () => void;
  onMessage?: (raw: string) => void;
  onClose?: (info: TransportCloseInfo) => void;
  onError?: (message: string) => void;
}

export type TransportStatus = "closed" | "connecting" | "open";

/** Swappable transport contract (WebSocket today, anything else tomorrow). */
export interface Transport {
  readonly status: TransportStatus;
  connect(url: string): void;
  send(raw: string): void;
  close(): void;
  setHandlers(handlers: TransportHandlers): void;
}
