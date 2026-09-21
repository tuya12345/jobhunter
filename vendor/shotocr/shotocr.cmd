@echo off
rem 截图 OCR 启动脚本（Windows）
rem 用法：shotocr --region 100,100,900,600
rem      shotocr --clipboard
rem      shotocr --file samples\demo.png --out-dir out
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"
"%ROOT%.venv\Scripts\python.exe" -m shotocr %*
exit /b %ERRORLEVEL%
