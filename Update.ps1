[CmdletBinding()]
param(
    [AllowEmptyString()][string]$ProxyUrl,
    [switch]$SkipRegistration
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.git'))) {
    throw 'For ZIP installations, extract the latest release and run Install.ps1 again.'
}
if (-not $PSBoundParameters.ContainsKey('ProxyUrl')) {
    $internet = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
    if ($internet.ProxyEnable -eq 1 -and $internet.ProxyServer) {
        $ProxyUrl = ($internet.ProxyServer -split ';' | Where-Object { $_ -match '^http=' } | Select-Object -First 1) -replace '^http=', ''
        if (-not $ProxyUrl) { $ProxyUrl = ($internet.ProxyServer -split ';')[0] -replace '^[a-z]+=', '' }
        if ($ProxyUrl -notmatch '^https?://') { $ProxyUrl = 'http://' + $ProxyUrl }
    }
}
$gitArguments = @('-C', $PSScriptRoot)
if ($ProxyUrl) { $gitArguments += @('-c', "http.proxy=$ProxyUrl") }
& git @gitArguments pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'Git update failed. Resolve it before rerunning Update.ps1.' }
& (Join-Path $PSScriptRoot 'Install.ps1') -ProxyUrl $ProxyUrl -SkipRegistration:$SkipRegistration
