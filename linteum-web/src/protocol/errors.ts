import { type ErrorInfo } from "./envelope";

/** A failed command, carrying the gateway's structured `error_info`. */
export class GatewayCommandError extends Error {
  readonly errorInfo: ErrorInfo;

  constructor(errorInfo: ErrorInfo) {
    super(errorInfo.message || errorInfo.code);
    this.name = "GatewayCommandError";
    this.errorInfo = errorInfo;
  }
}

export function localError(code: string, message: string): ErrorInfo {
  return { code, source: "client", message };
}

export function errorText(error: ErrorInfo | null | undefined): string {
  if (!error) return "";
  const detail = error.detail && error.detail !== error.message ? ` — ${error.detail}` : "";
  return `${error.code}: ${error.message}${detail}`;
}

export function isRetryable(error: ErrorInfo | null | undefined): boolean {
  return error?.retryable === true;
}
