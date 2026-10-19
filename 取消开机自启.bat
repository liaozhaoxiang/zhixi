@echo off
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ZhiXi.vbs"
if exist "%VBS%" (
  del /q "%VBS%"
  echo   已取消开机自启。
) else (
  echo   当前本来就没设置开机自启。
)
echo.
pause
