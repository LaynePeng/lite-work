# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""个人任务落盘区（Goals 全局视图）：跨平台 ~/lite-work/personal。

可见目录（与隐藏的数据目录 ~/.lite-work 区分：日志/会话在数据目录，产物在
可见目录）。macOS: /Users/<name>/lite-work/personal；
Windows: %USERPROFILE%\\lite-work\\personal。

「个人」是默认落盘位置而非伪项目：发邮件/整理照片这类不隶属任何项目的任务
的产物落在这里，任务卡徽标显示 👤 个人。
"""
from __future__ import annotations

import os

#: 个人落盘区绝对路径（模块级常量：多处引用需一致）
PERSONAL_WORKSPACE = os.path.join(
    os.path.expanduser("~"), "lite-work", "personal")


def ensure_personal_workspace() -> str:
    """确保个人落盘区存在（惰性创建，幂等）。返回其绝对路径。"""
    os.makedirs(PERSONAL_WORKSPACE, exist_ok=True)
    return PERSONAL_WORKSPACE
