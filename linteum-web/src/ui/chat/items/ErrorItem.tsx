import { AlertTriangle } from "lucide-react";
import type { ErrorItem as ErrorItemModel } from "../../../state/types";

export function ErrorItemView({ item }: { item: ErrorItemModel }) {
  return (
    <div className="error-card">
      <AlertTriangle size={16} style={{ flex: "none", marginTop: 2 }} />
      <div>
        <div className="error-card-title">{item.errorInfo.code}</div>
        <div>{item.errorInfo.message}</div>
        {item.errorInfo.detail && item.errorInfo.detail !== item.errorInfo.message ? (
          <div className="error-card-detail">{item.errorInfo.detail}</div>
        ) : null}
      </div>
    </div>
  );
}
