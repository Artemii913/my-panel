# Сборка «Моей панели»: MyPanel.exe (PyInstaller) + установщик (Inno Setup).
# Версия — из panel.py (VERSION). Результат: release\MyPanel-Setup-<версия>.exe
$ErrorActionPreference = "Continue"   # PyInstaller пишет журнал в stderr — это не ошибка; смотрим на код завершения
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Split-Path -Parent $here
$ver = (Select-String -Path "$src\panel.py" -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Собираю Моя панель $ver" -ForegroundColor Cyan
& py -3.12 -m PyInstaller "$src\panel.py" --name MyPanel --noconsole --onedir --noconfirm --clean `
  --icon "$here\icon.ico" --add-data "$src\ui.html;." --hidden-import shelf --hidden-import overlay --paths "$src" `
  --collect-submodules winrt --collect-data tzdata --collect-submodules recurring_ical_events --collect-submodules icalendar `
  --collect-submodules pycaw --collect-submodules comtypes --exclude-module numpy `
  --distpath "$src\dist" --workpath "$src\work" --specpath "$src\work"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 не найден" }
Push-Location $here
& $iscc "/DAppVer=$ver" "/DDist=$src\dist\MyPanel" "/DOutDir=$src\release" "MyPanel.iss"
Pop-Location
if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }
Write-Host "Готово: $src\release\MyPanel-Setup-$ver.exe" -ForegroundColor Green
