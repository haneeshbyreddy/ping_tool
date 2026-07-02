import { Component, type ReactNode } from "react"
import { Button } from "@/components/ui/button"

interface State { error: Error | null }

// A top-level safety net — without this, any render-time bug (like a missing cmdk
// provider) takes the whole app down to a blank screen with no way back short of a
// manual reload. Caught one of exactly this shape while verifying the command palette.
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
