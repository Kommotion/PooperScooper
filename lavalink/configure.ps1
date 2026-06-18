# Optional legacy helper. The bot now generates application.yml on startup from
# application.yml.template + config.json. Use this only if you run Lavalink manually.
# Syncs Spotify + Lavalink password from ../config.json into application.yml
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path (Split-Path $dir -Parent) "config.json"
$ymlPath = Join-Path $dir "application.yml"

if (-not (Test-Path $configPath)) {
    Write-Error "config.json not found at $configPath"
}
if (-not (Test-Path $ymlPath)) {
    Write-Error "application.yml not found at $ymlPath"
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json
$yml = Get-Content $ymlPath -Raw

$yml = $yml -replace 'clientId: "REPLACE_WITH_SPOTIFY_CLIENT_ID"', "clientId: `"$($config.spotify_client_id)`""
$yml = $yml -replace 'clientSecret: "REPLACE_WITH_SPOTIFY_SECRET"', "clientSecret: `"$($config.spotify_secret)`""
if ($config.lavalink_password) {
    $yml = $yml -replace 'password: "pooperscooper"', "password: `"$($config.lavalink_password)`""
}

$ytdlp = (Get-Command yt-dlp -ErrorAction SilentlyContinue).Source
$wrapperPath = Join-Path $dir "yt-dlp-wrapper.bat"
if ($ytdlp) {
    $wrapperContent = "@echo off`r`nset PYTHONWARNINGS=ignore`r`nset PYTHONIOENCODING=utf-8`r`n`"$ytdlp`" %*`r`n"
    Set-Content -Path $wrapperPath -Value $wrapperContent -Encoding ASCII
    $wrapperYaml = $wrapperPath -replace '\\', '/'
    $yml = $yml -replace 'path: "[^"]*"', "path: `"$wrapperYaml`""
    Write-Host "Set yt-dlp wrapper to $wrapperYaml"
} else {
    Write-Warning "yt-dlp not found on PATH - Lavalink may fail to search YouTube"
}

Set-Content -Path $ymlPath -Value $yml -NoNewline
Write-Host "Updated application.yml from config.json"