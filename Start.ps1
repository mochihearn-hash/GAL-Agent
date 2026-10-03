# 面向用户的源码启动入口：缺少环境时先初始化，再由 npm start 构建前端并启动 Electron。
# 无论启动正常结束还是脚本异常，都通过 finally 恢复执行前的工作目录。
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (!(Test-Path -LiteralPath '.venv\Scripts\python.exe') -or !(Test-Path -LiteralPath 'node_modules')) {
        & .\scripts\Setup.ps1
    }
    npm start
} finally { Pop-Location }
