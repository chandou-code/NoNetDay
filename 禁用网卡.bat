@echo off
chcp 65001 >nul
REM 禁用网卡脚本 - 需以管理员身份运行
REM 网卡名请改成 netsh interface show interface 查到的名字
set ADAPTER=WLAN

netsh interface set interface "%ADAPTER%" admin=disable
if %errorlevel%==0 (
    echo [%date% %time%] 已禁用网卡：%ADAPTER%
) else (
    echo [%date% %time%] 禁用失败，请检查是否以管理员身份运行，或网卡名是否正确
)
