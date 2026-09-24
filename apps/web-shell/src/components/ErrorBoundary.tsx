import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** page = 全局兜底（整页重载入口）；card = 卡片/区块级（就地重试，不拖垮页面）。 */
  variant?: "page" | "card";
}

interface State {
  error: Error | null;
}

/**
 * 渲染错误边界（F5-2）：page 变体兜底整页（保留重载入口而非白屏）；
 * card 变体下沉到插槽/区块，单卡崩溃只显示错误态，支持就地重试与复制诊断信息。
 */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[steward] 渲染异常：", error, info.componentStack);
  }

  private readonly reset = () => this.setState({ error: null });

  private readonly copyDiagnostics = () => {
    const error = this.state.error;
    const text = `[steward] ${new Date().toISOString()}\n${error?.message ?? ""}\n${error?.stack ?? ""}`;
    void navigator.clipboard?.writeText(text).catch(() => undefined);
  };

  override render() {
    if (!this.state.error) return this.props.children;
    if (this.props.variant === "card") {
      return (
        <div className="card-error" role="alert">
          <strong>这一块渲染出现异常</strong>
          <span>{this.state.error.message || "未知错误"}</span>
          <div className="card-error-actions">
            <button className="text-button" onClick={this.reset}>重试</button>
            <button className="text-button" onClick={this.copyDiagnostics}>复制诊断信息</button>
          </div>
        </div>
      );
    }
    return (
      <div className="booting-pane error-pane" role="alert">
        <strong>界面渲染出现异常</strong>
        <span>{this.state.error.message || "未知错误"}</span>
        <span className="hint-wide">
          数据仍保存在本地 Core，重新加载界面即可恢复；若反复出现请查看控制台日志并向维护者反馈。
        </span>
        <button className="primary-button" onClick={() => window.location.reload()}>
          重新加载界面 <span>→</span>
        </button>
      </div>
    );
  }
}
