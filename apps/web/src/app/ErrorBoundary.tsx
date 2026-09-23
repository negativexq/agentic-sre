import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
}

/**
 * Top-level error boundary: a render error shows a recoverable message instead
 * of a blank page. Errors are logged to the console for the observability story.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Console render error", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="mx-auto max-w-md px-4 py-16 text-center">
          <h1 className="text-lg font-semibold text-text">Something went wrong.</h1>
          <p className="mt-2 text-sm text-muted">
            The console hit an unexpected error while rendering this view.
          </p>
          <button
            onClick={() => this.setState({ error: null })}
            className="mt-4 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
