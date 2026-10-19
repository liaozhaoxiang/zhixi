@echo off
cd /d "%~dp0"
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ZhiXi.vbs"
if exist "%VBS%" del /q "%VBS%"
>> "%VBS%" echo Set s = CreateObject("WScript.Shell")
>> "%VBS%" echo s.Run """%~dp0daemon.bat""", 0, False
echo.
echo   OK - 已设置开机自启。
echo   下次登录 Windows 会在后台自动启动智析（不弹窗口、不弹页面）。
echo   想取消就双击 [取消开机自启.bat]
echo.
pause
