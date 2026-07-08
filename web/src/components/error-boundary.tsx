import { Component, type ReactNode } from "react"
import { Button } from "@/components/ui/button"

interface State { error: Error | null }

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: { componentStack?: string | null }) {
    console.error("[wisp] render error:", error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-svh flex-col items-center justify-center gap-3 bg-background p-6 text-center">
          <p className="text-sm font-semibold text-foreground">Something went wrong.</p>
          <p className="max-w-sm text-xs text-muted-foreground">{this.state.error.message}</p>
          <Button size="sm" onClick={() => window.location.reload()}>Reload</Button>
        </div>
      )
    }
    return this.props.children
  }
}
