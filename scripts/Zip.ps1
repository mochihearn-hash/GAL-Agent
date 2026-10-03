# 接收打包脚本传入的明确源目录和目标文件；LiteralPath 不把路径中的特殊字符当通配符。
# ZIP 包含完整版本目录，解压后须保留可执行文件旁的 resources 等资源。
param([Parameter(Mandatory=$true)][string]$SourcePath,[Parameter(Mandatory=$true)][string]$DestinationPath)
$ErrorActionPreference = 'Stop'
Compress-Archive -LiteralPath $SourcePath -DestinationPath $DestinationPath -CompressionLevel Optimal -Force
