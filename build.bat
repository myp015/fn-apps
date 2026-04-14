@echo off
setlocal enabledelayedexpansion

REM .\fnpack.exe create fn-kodi -t docker --without-ui true
REM .\fnpack.exe build --directory fn-kodi

curl -kL https://static2.fnnas.com/fnpack/fnpack-1.0.4-windows-amd64 -o fnpack.exe

for /d %%A in (fn-*) do (
  if exist "%%A\norelease" (
    REM skip
    ) else if exist "%%A\manifest" (
    echo Building %%A ...
    
    REM 解析 appname 和 version，只取第一行
    set "APPNAME="
    set "VERSION="
    set "PLATFORM="
    for /f "tokens=2 delims==" %%i in ('findstr /i /r "^appname *=.*" "%%A\manifest"') do (
      if not defined APPNAME set "APPNAME=%%i"
    )
    for /f "tokens=2 delims==" %%i in ('findstr /i /r "^version *=.*" "%%A\manifest"') do (
      if not defined VERSION set "VERSION=%%i"
    )
    for /f "tokens=2 delims==" %%i in ('findstr /i /r "^platform *=.*" "%%A\manifest"') do (
      if not defined PLATFORM set "PLATFORM=%%i"
    )
    
    for /f "tokens=* delims= " %%i in ("!APPNAME!") do set "APPNAME=%%i"
    for /f "tokens=* delims= " %%i in ("!VERSION!") do set "VERSION=%%i"
    for /f "tokens=* delims= " %%i in ("!PLATFORM!") do set "PLATFORM=%%i"
    
    echo Building %%A ...
    
    if exist  "%%A\build.bat" (
      call "%%A\build.bat"
      ) else (
      .\fnpack.exe build --directory %%A
      if defined APPNAME if defined VERSION if exist "!APPNAME!.fpk" (
        move /y "!APPNAME!.fpk" "!APPNAME!_!PLATFORM!_v!VERSION!.fpk" >nul
      )
    )
  )
)
