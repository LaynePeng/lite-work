// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

// React.lazy 的薄封装：少写一层 default 解构。
//
// 为什么需要：主 chunk 曾把 xterm（约 335KB）与几个大 modal 一起静态打进首屏，
// 而它们往往整个会话都用不到一次——首屏解析时间全花在这些字节上。
// 动态 import 让 Vite 把它们拆成独立 chunk，用到才拉。
//
// P 由 factory 的返回类型推出（不写 any / never），组件 props 检查照常生效。

import { lazy, type ComponentType } from "react";

export function lazyChunk<P>(factory: () => Promise<{ default: ComponentType<P> }>) {
  return lazy(factory);
}
