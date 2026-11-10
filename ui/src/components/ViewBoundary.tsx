import { Component, type ReactNode } from "react";

/** A failed lazy bundle (for example an older open tab after deployment) must not blank the app. */
export class ViewBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return <section className="banner banner-error" role="alert">
      <h2>This view could not load</h2>
      <p>The application may have been updated, or a page resource could not be loaded. Reload to get the current version.</p>
      <button type="button" onClick={() => window.location.reload()}>Reload page</button>
    </section>;
  }
}
