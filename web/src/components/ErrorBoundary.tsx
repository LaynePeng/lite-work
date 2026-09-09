// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import React from "react";

interface Props {
  children: React.ReactNode;
  /** 兜底区域标签（分层兜底时定位崩溃区域，如「侧边栏」「聊天区」） */
  name?: string;
  /** 紧凑形态：面板/侧边栏局部兜底（不带标题，占位更小） */
  compact?: boolean;
}

interface State {
  hasError: boolean;
  message: string;
}

/**
 * 分层错误边界：子组件崩溃按区域兜底不白屏。
 */
export default class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, message: "" };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message || String(error) };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(`[ErrorBoundary${this.props.name ? `·${this.props.name}` : ""}] 渲染异常:`, error, info);
  }

  private retry = () => {
    this.setState({ hasError: false, message: "" });
  };

  render() {
    if (this.state.hasError) {
      const label = this.props.name ? `${this.props.name} ` : "";
      if (this.props.compact) {
        return (
          <div className="boundary-fallback compact" role="alert">
            <span className="boundary-msg" title={this.state.message}>
              ⚠ {label}渲染出错
            </span>
            <button className="boundary-retry" onClick={this.retry}>重试</button>
          </div>
        );
      }
      return (
        <div className="crash-screen">
          <div className="crash-icon">💥</div>
          <h2>界面渲染出错{this.props.name ? `（${this.props.name}）` : ""}</h2>
          <p className="crash-message">{this.state.message}</p>
          <div className="crash-actions">
            <button onClick={this.retry}>🔄 重试恢复</button>
            <button onClick={() => window.location.reload()}>重新加载页面</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
