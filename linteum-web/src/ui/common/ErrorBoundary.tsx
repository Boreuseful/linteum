import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  label?: string;
}

interface State {
  error: Error | null;
}

/** Keeps one broken right-side panel from taking down the transcript. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("panel crashed", this.props.label, error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="panel-empty">
          {this.props.label ?? "Panel"} failed: {this.state.error.message}
        </div>
      );
    }
    return this.props.children;
  }
}
