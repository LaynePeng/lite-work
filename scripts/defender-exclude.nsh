; lite-work Windows Defender 排除项注册
; 安装时：将安装目录加入 Defender 实时扫描排除列表，消除冷启动时
;         扫描数百 MB 后端 DLL（onnxruntime / cv2 / pymupdf 等）的延迟。
; 卸载时：移除排除项，不留下残留。
; 依赖：perMachine 安装（管理员权限），否则 Add-MpPreference 静默跳过。
;
; NSIS 语法注意：
; - 字符串不支持 ^ 跨行续行（CMD 语法），命令必须单行
; - 外层用反引号字符串，内层 PS 单引号包路径（$INSTDIR 含空格也不截断）

!macro customInstall
  DetailPrint "正在配置 Windows Defender 排除项…"
  nsExec::ExecToLog `powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath '$INSTDIR' -ErrorAction SilentlyContinue"`
  Pop $0
  IntCmp $0 0 def_ok def_fail def_fail
def_ok:
  DetailPrint "Defender 排除项已添加: $INSTDIR"
  Goto def_done
def_fail:
  DetailPrint "Defender 排除项注册失败（非管理员或系统策略阻止），不影响安装完成。"
def_done:
!macroend

!macro customUnInstall
  DetailPrint "正在移除 Windows Defender 排除项…"
  nsExec::ExecToLog `powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Remove-MpPreference -ExclusionPath '$INSTDIR' -ErrorAction SilentlyContinue"`
  Pop $0
!macroend
