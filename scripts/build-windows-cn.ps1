# CN-local packaging wrapper: load CN mirrors, then run the standard Windows build.
. (Join-Path $PSScriptRoot "mirrors.ps1")
& (Join-Path $PSScriptRoot "build-windows.ps1") @args
exit $LASTEXITCODE
