param([string]$Owner = "ssaaach", [string]$Repository = "AI-Chaos-Pilot", [string]$Branch = "feat/sachin-integration")
$ErrorActionPreference = "Stop"
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
gh auth status
if ($LASTEXITCODE -ne 0) { throw "Use normal gh auth login first. This script never reads credential files directly." }
gh repo view "$Owner/$Repository" --json url
if ($LASTEXITCODE -ne 0) { throw "The existing team repository is unavailable; no new repository will be created." }
git -c "safe.directory=$taskRoot" rev-parse --verify HEAD
if ($LASTEXITCODE -ne 0) { throw "Create and review a local source commit before publishing." }
git -c "safe.directory=$taskRoot" push "https://github.com/$Owner/$Repository.git" "HEAD:refs/heads/$Branch"
if ($LASTEXITCODE -ne 0) { throw "Branch upload not confirmed." }
Write-Host "Feature branch published. Open a pull request for teammate review."
