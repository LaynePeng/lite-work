// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

/** 应用图标：颜色取 CSS 变量（随 UI 主题 classic/swiss 切换）。
 *  经典 = 蓝紫渐变底 + 白闪电齿轮；瑞士 = 纸白底 + 墨黑 + 红方块。 */
export default function AppIcon({ size = 30 }: { size?: number }) {
  return (
    <svg viewBox="0 0 1024 1024" width={size} height={size} xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="app-icon-bg" x1="0" y1="0" x2="1024" y2="1024" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="var(--accent)" />
          <stop offset="1" stopColor="var(--accent-2)" />
        </linearGradient>
      </defs>
      <rect x="0" y="0" width="1024" height="1024" fill="var(--text-0)" />
      <rect x="40" y="40" width="944" height="944" fill="var(--bg-0)" />
      <rect x="40" y="40" width="200" height="200" fill="var(--accent)" />
      <path d="M635 90 L370 440 L465 440 L355 670 L710 360 L590 360 L695 90 Z" fill="var(--text-0)" />
      <g transform="translate(512 830)">
        <rect x="-20" y="-160" width="40" height="85" fill="var(--text-0)" />
        <rect x="-20" y="-160" width="40" height="85" fill="var(--text-0)" transform="rotate(45)" />
        <rect x="-20" y="-160" width="40" height="85" fill="var(--text-0)" transform="rotate(90)" />
        <rect x="-20" y="-160" width="40" height="85" fill="var(--text-0)" transform="rotate(135)" />
        <rect x="-20" y="-160" width="40" height="85" fill="var(--text-0)" transform="rotate(180)" />
        <rect x="-20" y="-160" width="40" height="85" fill="var(--text-0)" transform="rotate(225)" />
        <rect x="-20" y="-160" width="40" height="85" fill="var(--text-0)" transform="rotate(270)" />
        <rect x="-20" y="-160" width="40" height="85" fill="var(--text-0)" transform="rotate(315)" />
        <circle r="105" fill="var(--text-0)" />
        <circle r="42" fill="var(--bg-0)" />
      </g>
    </svg>
  );
}
