@echo off
cd /d "C:\Users\manum\Desktop\IA Projects\podcaster"

echo [%DATE% %TIME%] Pipeline starting... >> logs\scheduler.log

python pipeline_runner.py --publish >> logs\scheduler.log 2>&1
set EXIT_CODE=%ERRORLEVEL%

echo [%DATE% %TIME%] Pipeline finished with exit code %EXIT_CODE% >> logs\scheduler.log

exit /b %EXIT_CODE%
