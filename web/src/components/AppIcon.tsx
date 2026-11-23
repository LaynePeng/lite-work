// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

/** 应用图标（瑞士风格：纸白底 + 墨黑描边 + 红方块 + 闪电 ⚡ + 齿轮 ⚙） */
export default function AppIcon({ size = 30 }: { size?: number }) {
  return (
    <svg viewBox="0 0 1024 1024" width={size} height={size} xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <rect x="0" y="0" width="1024" height="1024" rx="0" fill="#111111" />
      <rect x="40" y="40" width="944" height="944" fill="#f5f4f0" />
      <rect x="40" y="40" width="200" height="200" fill="#e63312" />
      <path d="M635 90 L370 440 L465 440 L355 670 L710 360 L590 360 L695 90 Z" fill="#111111" />
      <g transform="translate(512 830)" fill="#111111">
        <rect x="-20" y="-160" width="40" height="85" rx="0" fill="#111111" />
        <rect x="-20" y="-160" width="40" height="85" rx="0" fill="#111111" transform="rotate(45)" />
        <rect x="-20" y="-160" width="40" height="85" rx="0" fill="#111111" transform="rotate(90)" />
        <rect x="-20" y="-160" width="40" height="85" rx="0" fill="#111111" transform="rotate(135)" />
        <rect x="-20" y="-160" width="40" height="85" rx="0" fill="#111111" transform="rotate(180)" />
        <rect x="-20" y="-160" width="40" height="85" rx="0" fill="#111111" transform="rotate(225)" />
        <rect x="-20" y="-160" width="40" height="85" rx="0" fill="#111111" transform="rotate(270)" />
        <rect x="-20" y="-160" width="40" height="85" rx="0" fill="#111111" transform="rotate(315)" />
        <circle r="105" fill="#111111" />
        <circle r="42" fill="#f5f4f0" />
      </g>
    </svg>
  );
}