<#
部署 KOOK 定时打卡（v7 onedir）到用户目录。
安全约束（吸取 2026-11-12 事故教训）：
  * 只允许目标目录末段等于 -AppFolderName（默认 KOOK打卡），否则直接报错退出；
  * 绝不对目标父目录使用 robocopy /MIR；同步用 /E 覆盖复制；
  * 本脚本以 UTF-8 BOM 保存，保证 PowerShell 5.1 正确解析中文。
用法：
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\deploy.ps1
  powershell ... -File tools\deploy.ps1 -Dist "dist\KOOK打卡" -DryRun
#>
param(
  [string]$Dist = 'dist\KOOK打卡',
  [string]$Target = '',
  [string]$AppFolderName = 'KOOK打卡',
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$srcPath = Join-Path $repo $Dist
if (-not (Test-Path $srcPath)) { throw "dist 目录不存在: $Dist（先运行 python build.py）" }
$src = (Resolve-Path $srcPath).Path
if (-not $Target) { $Target = Join-Path $env:LOCALAPPDATA ('Programs\' + $AppFolderName) }
$Target = [System.IO.Path]::GetFullPath($Target)

# --- 安全检查：目标末段必须精确等于应用目录名 ---
$leaf = Split-Path -Leaf $Target
if ($leaf -ne $AppFolderName) { throw "拒绝执行：目标 '$Target' 末段不是 '$AppFolderName'" }
$exeName = $AppFolderName + '.exe'
if (-not (Test-Path (Join-Path $src $exeName))) { throw "源目录缺少 $exeName : $src" }

Write-Host "源  : $src"
Write-Host "目标: $Target"
if ($DryRun) { Write-Host '[DryRun] 不做任何改动'; exit 0 }

# --- 关闭正在运行的旧实例 ---
# 先请它自己退出（v7.2 起支持 --quit：写 quit.flag，实例 2 秒内自己收尾、关浏览器、写完日志），
# 15 秒还不走才强杀。强杀会留下锁住的 kook_profile 和一个没写完的日志尾巴。
$running = Get-Process -Name $AppFolderName -ErrorAction SilentlyContinue
if ($running) {
  $oldExe = Join-Path $Target $exeName
  if (Test-Path $oldExe) {
    Write-Host '请求旧实例退出（--quit）...'
    try { & $oldExe --quit | Out-Null } catch { }
    for ($i = 0; $i -lt 30; $i++) {
      if (-not (Get-Process -Name $AppFolderName -ErrorAction SilentlyContinue)) { break }
      Start-Sleep -Milliseconds 500
    }
  }
  if (Get-Process -Name $AppFolderName -ErrorAction SilentlyContinue) {
    Get-Process -Name $AppFolderName | Stop-Process -Force
    Start-Sleep -Milliseconds 800
    Write-Host '旧实例未响应退出请求，已强制结束'
  } else {
    Write-Host '旧实例已退出'
  }
}

# --- 同步文件（/E 覆盖复制，绝不用 /MIR） ---
$argList = @(('"' + $src + '"'), ('"' + $Target + '"'), '/E', '/R:2', '/W:2', '/NFL', '/NDL', '/NJH', '/NJS')
$robo = Start-Process -FilePath 'robocopy.exe' -ArgumentList $argList -Wait -PassThru -NoNewWindow
if ($robo.ExitCode -ge 8) { throw ('robocopy 失败，退出码 ' + $robo.ExitCode) }

$exe = Join-Path $Target $exeName

# --- 桌面快捷方式 ---
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'KOOK 定时打卡.lnk'
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $exe
$sc.WorkingDirectory = $Target
$sc.IconLocation = "$exe,0"
$sc.Save()
Write-Host "快捷方式: $lnk"

# --- 开机自启 ---
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
New-ItemProperty -Path $runKey -Name 'KOOKCheckin' -Value ('"' + $exe + '" --auto') -PropertyType String -Force | Out-Null
Write-Host "已写入开机自启: HKCU Run\KOOKCheckin"
Write-Host "部署完成。"