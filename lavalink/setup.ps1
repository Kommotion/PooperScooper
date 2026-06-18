# Downloads Lavalink server jar (run once). Requires Java 17+ to run Lavalink.
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

$lavalinkVersion = "4.2.2"
$jarName = "Lavalink.jar"
$url = "https://github.com/lavalink-devs/Lavalink/releases/download/$lavalinkVersion/$jarName"

if (Test-Path $jarName) {
    Write-Host "$jarName already exists - skipping download."
    exit 0
}

Write-Host "Downloading Lavalink $lavalinkVersion..."
Invoke-WebRequest -Uri $url -OutFile $jarName
Write-Host "Done. Run configure.ps1 then start.bat"