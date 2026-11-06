// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

// React.lazy 的薄封装：少写一层 default 解构。
//
// 为什么需要：主 chunk 曾把 xterm（约 300KB）与几个大 modal 一起静态打进首屏，
// 而它们往往整个会话都用不到一次——首屏解析时间全花在这些字节上。
// 动态 import 让 Vite 把它们拆成独立 chunk，用到才拉。

import { lazy, type ComponentType } from "react";

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 透传组件的 props 类型
export function lazyChunk<T extends ComponentType<any>>(
  factory: () => Promise<{ default: T }>,
) {
  return lazy(factory);
}
