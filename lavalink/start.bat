@echo off
cd /d "%~dp0"
if not exist "Lavalink.jar" (
    echo Lavalink.jar not found. Run setup.ps1 first.
    exit /b 1
)
echo Starting Lavalink on port 2333...
java -jar Lavalink.jar