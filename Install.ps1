[CmdletBinding()]
param(
    [string]$Distro = '',
    [string]$RuntimeDirectory = '',
    [string]$StateDirectory = '',
    [string]$SshConfig = '',
    [string[]]$Hosts = @(),
    [string[]]$MfaHosts = @('8HFA40', 'hf6-A800', 'hf7-A40'),
    [string]$PasswordHost = 'HF3090',
    [AllowEmptyString()][string]$ProxyUrl,
    [string]$IndexUrl = 'https://pypi.org/simple',
    [switch]$SkipRegistration
)
$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$InstallDirectory = Join-Path $env:LOCALAPPDATA 'CodexSshTerminal'
$settingsFile = Join-Path $InstallDirectory 'runtime.json'
$previous = $null
if (Test-Path -LiteralPath $settingsFile) {
    $previous = Get-Content -LiteralPath $settingsFile -Raw | ConvertFrom-Json
    if (-not $PSBoundParameters.ContainsKey('Distro')) { $Distro = $previous.distro }
    if (-not $PSBoundParameters.ContainsKey('RuntimeDirectory')) { $RuntimeDirectory = $previous.runtime }
    if (-not $PSBoundParameters.ContainsKey('StateDirectory')) { $StateDirectory = $previous.state }
    if (-not $PSBoundParameters.ContainsKey('SshConfig') -and $previous.sshConfig) { $SshConfig = $previous.sshConfig }
    if (-not $PSBoundParameters.ContainsKey('MfaHosts')) { $MfaHosts = @($previous.mfaHosts) }
    if (-not $PSBoundParameters.ContainsKey('PasswordHost')) { $PasswordHost = $previous.passwordHost }
}

function Save-Json($Path, $Value) {
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12), $utf8)
}

function Invoke-Wsl {
    param([string[]]$Arguments)
    $result = & wsl.exe -d $Distro --exec @Arguments
    if ($LASTEXITCODE -ne 0) {
        $result | Write-Host
        throw 'WSL command failed. Check the preceding error.'
    }
    return $result
}

function Convert-WslPath([string]$Path) {
    return (Invoke-Wsl -Arguments @('wslpath', '-u', $Path) | Out-String).Trim()
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'Install WSL first using: wsl --install -d Ubuntu-24.04'
}
$node = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $node) { throw 'Install Node.js 20+ first using: winget install OpenJS.NodeJS.LTS' }
$nodeInfo = (& $node.Source -p 'JSON.stringify({version:process.versions.node,path:process.execPath})') | ConvertFrom-Json
if ([int]$nodeInfo.version.Split('.')[0] -lt 20) { throw 'Node.js 20 or newer is required.' }
if (-not $Distro) {
    $Distro = (& wsl.exe --exec printenv WSL_DISTRO_NAME | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Distro) { throw 'Initialize an Ubuntu WSL distribution first.' }
}
$wslHome = (Invoke-Wsl -Arguments @('printenv', 'HOME') | Out-String).Trim()
if (-not $RuntimeDirectory) { $RuntimeDirectory = "$wslHome/.local/share/codex-ssh-terminal-mcp" }
if (-not $StateDirectory) { $StateDirectory = "$wslHome/.config/codex-ssh-terminal-mcp" }
if (-not $SshConfig) { $SshConfig = Join-Path $env:USERPROFILE '.ssh\config' }
if (-not $PSBoundParameters.ContainsKey('ProxyUrl')) {
    $internet = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
    if ($internet.ProxyEnable -eq 1 -and $internet.ProxyServer) {
        $ProxyUrl = ($internet.ProxyServer -split ';' | Where-Object { $_ -match '^http=' } | Select-Object -First 1) -replace '^http=', ''
        if (-not $ProxyUrl) { $ProxyUrl = ($internet.ProxyServer -split ';')[0] -replace '^[a-z]+=', '' }
        if ($ProxyUrl -notmatch '^https?://') { $ProxyUrl = 'http://' + $ProxyUrl }
    }
}
if ($MfaHosts.Count -ne 3 -or (@($MfaHosts | Select-Object -Unique)).Count -ne 3 -or $PasswordHost -in $MfaHosts) {
    throw 'Choose three distinct MFA aliases and a separate password-only alias.'
}
New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
$InstallDirectory = (Resolve-Path -LiteralPath $InstallDirectory).Path
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'scripts\start-mcp.cjs') -Destination (Join-Path $InstallDirectory 'start-mcp.cjs')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-tcp-proxy.cjs') -Destination (Join-Path $InstallDirectory 'windows-tcp-proxy.cjs')

$resolvedHosts = [System.Collections.Generic.List[object]]::new()
if (Test-Path -LiteralPath $SshConfig) {
    $ssh = Get-Command ssh.exe -ErrorAction Stop
    if (-not $Hosts.Count) {
        $aliases = foreach ($line in Get-Content -LiteralPath $SshConfig) {
            if ($line -match '^\s*Host\s+([^#]+)') { $Matches[1].Trim() -split '\s+' | Where-Object { $_ -match '^[A-Za-z0-9][A-Za-z0-9_.-]*$' } }
        }
        $Hosts = @($aliases | Select-Object -Unique)
    }
    foreach ($alias in $Hosts) {
        if ($alias -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$') { throw 'SSH aliases must contain letters, numbers, dots, underscores or hyphens.' }
        $effective = & $ssh.Source -G -F $SshConfig $alias 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Cannot resolve SSH alias $alias" }
        $values = @{}
        $identityFiles = @()
        foreach ($line in $effective) {
            $pair = $line -split '\s+', 2
            if ($pair.Count -ne 2) { continue }
            if ($pair[0] -eq 'identityfile') {
                $identity = $pair[1] -replace '^~', $env:USERPROFILE
                if (Test-Path -LiteralPath $identity -PathType Leaf) { $identityFiles += $identity }
            } else { $values[$pair[0]] = $pair[1] }
        }
        $jump = $values['proxyjump']
        if ($jump -eq 'none') { $jump = '' }
        $resolvedHosts.Add(@{name=$alias;host=$values['hostname'];user=$values['user'];port=[int]$values['port'];proxyJump=$jump;
            identityFiles=$identityFiles;PubkeyAuthentication=$values['pubkeyauthentication'];PreferredAuthentications=$values['preferredauthentications']})
    }
}
$settings = @{
    distro=$Distro;runtime=$RuntimeDirectory;state=$StateDirectory;sshConfig=$SshConfig;hosts=@($resolvedHosts.ToArray());
    mfaHosts=$MfaHosts;passwordHost=$PasswordHost;nodeWsl=(Convert-WslPath $nodeInfo.path);
    relayWindows=(Join-Path $InstallDirectory 'windows-tcp-proxy.cjs');
    allowlistWindows=(Join-Path $InstallDirectory 'relay-allowlist.json');
    powershell=(Convert-WslPath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'))
}
Save-Json $settingsFile $settings
$pipFile = Join-Path $InstallDirectory 'pip.pyz'
$download = @{Uri='https://bootstrap.pypa.io/pip/pip.pyz';OutFile=$pipFile;UseBasicParsing=$true}
if ($ProxyUrl) { $download.Proxy = $ProxyUrl }
Invoke-WebRequest @download
$arguments = @('python3', (Convert-WslPath (Join-Path $PSScriptRoot 'scripts\install_runtime.py')),
    '--settings', (Convert-WslPath $settingsFile), '--pip', (Convert-WslPath $pipFile), '--index-url', $IndexUrl)
if ($ProxyUrl) { $arguments += @('--proxy', $ProxyUrl) }
Invoke-Wsl -Arguments $arguments

if (-not $SkipRegistration) {
    $codex = Get-Command codex.exe -ErrorAction SilentlyContinue
    if (-not $codex) {
        $codex = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin\*\codex.exe') -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    }
    if (-not $codex) { throw 'Codex CLI was not found. Open Codex App once, then rerun Install.ps1.' }
    $codexPath = if ($codex.Source) { $codex.Source } else { $codex.FullName }
    # Register a stable local source so ZIP upgrades and moved checkouts work.
    $marketplaceRoot = Join-Path $InstallDirectory 'marketplace'
    foreach ($relative in @('.agents\plugins\marketplace.json', '.codex-plugin\plugin.json', '.mcp.json')) {
        $target = Join-Path $marketplaceRoot $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $relative) -Destination $target
    }
    $marketplaces = & $codexPath plugin marketplace list --json
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect existing plugin marketplaces.' }
    $existing = ($marketplaces | Out-String | ConvertFrom-Json).marketplaces |
        Where-Object { $_.name -eq 'ssh-terminal-community' } | Select-Object -First 1
    if ($existing) {
        $existingRoot = [string]$existing.root
        if ($existingRoot.StartsWith('\\?\')) { $existingRoot = $existingRoot.Substring(4) }
        if ([System.IO.Path]::GetFullPath($existingRoot).TrimEnd('\') -ne $marketplaceRoot.TrimEnd('\')) {
            & $codexPath plugin marketplace remove ssh-terminal-community --json
            if ($LASTEXITCODE -ne 0) { throw 'Could not migrate the SSH marketplace registration.' }
        }
    }
    & $codexPath plugin marketplace add $marketplaceRoot --json
    if ($LASTEXITCODE -ne 0) { throw 'Could not register the plugin marketplace.' }
    & $codexPath plugin add ssh-terminal@ssh-terminal-community --json
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the SSH terminal plugin.' }
}
Write-Host 'SSH terminal installation completed. Open the plugin in a new Codex chat.'
Write-Host "Imported $($resolvedHosts.Count) SSH aliases. Fill passwords locally in $StateDirectory/passwords.json (WSL)."
