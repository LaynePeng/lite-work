// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 移动布局判定：窄屏 + 非 Electron。Electron 窗口可以被拖到很窄，但桌面
// 布局与行为是红线（与 1.10.10 一致），绝不因窗口宽度切换。
// 调试：URL 加 ?mobile=1 强制移动布局，?mobile=0 强制桌面布局。

import { useEffect, useState } from "react";
import { isElectron } from "../lib/connection";

const QUERY = "(max-width: 767px)";

export function useIsMobile(): boolean {
  const [narrow, setNarrow] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(QUERY).matches;
  });
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(QUERY);
    const onChange = () => setNarrow(mq.matches);
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener("change", onChange);
      else if (mq.removeListener) mq.removeListener(onChange);
    };
  }, []);
  const param = typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("mobile");
  if (param === "1") return true;
  if (param === "0") return false;
  return narrow && !isElectron();
}
