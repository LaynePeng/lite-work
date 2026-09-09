; lite-work Windows Defender 排除项注册
; 安装时：将安装目录加入 Defender 实时扫描排除列表，消除冷启动时
;         扫描数百 MB 后端 DLL（onnxruntime / cv2 / pymupdf 等）的延迟。
; 卸载时：移除排除项，不留下残留。
;
; 依赖：安装器需以管理员权限运行（perMachine: true），
;       否则 Add-MpPreference 会因权限不足静默跳过。

!macro customInstall
  DetailPrint "正在配置 Windows Defender 排除项…"
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "Add-MpPreference -ExclusionPath \"$INSTDIR\" -ErrorAction SilentlyContinue"'
  Pop $0
  ${If} $0 != 0
    DetailPrint "Defender 排除项注册失败（非管理员或系统策略阻止），不影响安装完成。"
  ${Else}
    DetailPrint "Defender 排除项已添加: $INSTDIR"
  ${EndIf}
!macroend

!macro customUnInstall
  DetailPrint "正在移除 Windows Defender 排除项…"
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "Remove-MpPreference -ExclusionPath \"$INSTDIR\" -ErrorAction SilentlyContinue"'
!macroend