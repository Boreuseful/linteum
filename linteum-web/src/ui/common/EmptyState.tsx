import type { ReactNode } from "react";

export function EmptyState({
  icon,
  title,
  children,
}: {
  icon?: ReactNode;
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {icon}
      {title ? <strong>{title}</strong> : null}
      {children}
    </div>
  );
}

export function PanelEmpty({ children }: { children: ReactNode }) {
  return <div className="panel-empty">{children}</div>;
}

export function PanelLoading({ label }: { label: string }) {
  return (
    <div className="panel-loading">
      <span className="spinner" />
      <span>{label}</span>
    </div>
  );
}
