// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

/** 应用图标（与 scripts/app-icon.svg 同源：渐变底 + 闪电 ⚡ + 齿轮 ⚙） */
export default function AppIcon({ size = 30 }: { size?: number }) {
  return (
    <svg viewBox="0 0 1024 1024" width={size} height={size} xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="app-icon-bg" x1="0" y1="0" x2="1024" y2="1024" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#4F8CFF" />
          <stop offset="0.55" stopColor="#6C7BFF" />
          <stop offset="1" stopColor="#8B5CF6" />
        </linearGradient>
        <linearGradient id="app-icon-gloss" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.30" />
          <stop offset="0.45" stopColor="#ffffff" stopOpacity="0.04" />
          <stop offset="1" stopColor="#ffffff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect x="0" y="0" width="1024" height="1024" rx="190" fill="url(#app-icon-bg)" />
      <rect x="0" y="0" width="1024" height="560" rx="190" fill="url(#app-icon-gloss)" />
      <path d="M635 90 L370 440 L465 440 L355 670 L710 360 L590 360 L695 90 Z" fill="#ffffff" />
      <g transform="translate(512 830)">
        <rect x="-20" y="-160" width="40" height="85" rx="10" fill="#ffffff" />
        <rect x="-20" y="-160" width="40" height="85" rx="10" fill="#ffffff" transform="rotate(45)" />
        <rect x="-20" y="-160" width="40" height="85" rx="10" fill="#ffffff" transform="rotate(90)" />
        <rect x="-20" y="-160" width="40" height="85" rx="10" fill="#ffffff" transform="rotate(135)" />
        <rect x="-20" y="-160" width="40" height="85" rx="10" fill="#ffffff" transform="rotate(180)" />
        <rect x="-20" y="-160" width="40" height="85" rx="10" fill="#ffffff" transform="rotate(225)" />
        <rect x="-20" y="-160" width="40" height="85" rx="10" fill="#ffffff" transform="rotate(270)" />
        <rect x="-20" y="-160" width="40" height="85" rx="10" fill="#ffffff" transform="rotate(315)" />
        <circle r="105" fill="#ffffff" />
        <circle r="42" fill="url(#app-icon-bg)" />
      </g>
    </svg>
  );
}