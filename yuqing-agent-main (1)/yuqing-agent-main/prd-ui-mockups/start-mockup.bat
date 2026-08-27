@echo off
title PRD 舆情原型
echo ============================================
echo   舆情分析系统 - 可交互原型
echo ============================================
echo.
echo 数据服务: http://127.0.0.1:8091/
echo 数据来自 data\articles.json 快照，无需 MySQL。
echo.
py server.py 8091
pause
