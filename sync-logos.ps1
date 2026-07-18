# EML Team Logo Sync for TouchPortal
# =====================================
# Put this in:  Documents\TouchPortal\sync-logos.ps1
# Run before your stream — it downloads the latest approved logos.
#
# One-time setup:
#   1. Save this file as Documents\TouchPortal\sync-logos.ps1
#   2. Create a TouchPortal button that runs this script (or just double-click it)
#   3. In TouchPortal, point image sources to Documents\TouchPortal\EML_TeamLogos\

$logoDir = "$env:USERPROFILE\Documents\TouchPortal\EML_TeamLogos"
$zipUrl  = "https://casterbot.onrender.com/api/team-logos.zip"

# Create the folder if it doesn't exist
New-Item -ItemType Directory -Force -Path $logoDir | Out-Null

# Download the ZIP
$zipPath = "$env:TEMP\eml-team-logos.zip"
Write-Host "Downloading team logos..."
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing

# Extract, overwriting existing files
Write-Host "Extracting to $logoDir..."
Expand-Archive -Path $zipPath -DestinationPath $logoDir -Force

# Clean up
Remove-Item $zipPath -Force

Write-Host "Done! Team logos synced to $logoDir"
Write-Host "They are now available in TouchPortal."
