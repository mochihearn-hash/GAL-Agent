# 首次源码环境初始化：项目内虚拟环境、锁定的 Python 依赖、npm 锁定依赖和前端构建。
# 使用项目根目录作为工作目录；完成或发生异常后都恢复用户原来的目录。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location $projectRoot
try {
    if (!(Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        # 只在缺少项目虚拟环境时新建，不修改系统 Python 安装或其他项目环境。
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw '创建独立 Python 环境失败' }
    }
    .\.venv\Scripts\python.exe -m pip install -r backend\requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw '安装后端依赖失败' }
    npm ci
    # npm ci 按 package-lock.json 安装，依赖版本以锁文件为准。
    if ($LASTEXITCODE -ne 0) { throw '安装前端依赖失败' }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
} finally { Pop-Location }
