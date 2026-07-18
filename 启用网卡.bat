@echo off
chcp 65001 >nul
REM 启用网卡脚本 - 需以管理员身份运行
set ADAPTER=WLAN

netsh interface set interface "%ADAPTER%" admin=enable
if %errorlevel%==0 (
    echo [%date% %time%] 已启用网卡：%ADAPTER%
) else (
    echo [%date% %time%] 启用失败，请检查是否以管理员身份运行，或网卡名是否正确
)
