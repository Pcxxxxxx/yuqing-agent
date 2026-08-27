@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Yuqing System - Update & Push to GitHub
echo ============================================
echo.

echo Pulling latest changes...
git pull

git add .
git commit -m "update" 2>nul

echo Pushing to GitHub...
git push

echo.
echo Done.
pause
