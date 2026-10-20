# Executed as a local encoded command, without changing execution policy.
# $violetPlanJson is a base64-decoded JSON value supplied by update-install.cjs.
$ErrorActionPreference = 'Stop'
$plan = $violetPlanJson | ConvertFrom-Json
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Write-Status([string] $value) {
    [IO.File]::WriteAllText((Join-Path $plan.transaction 'status'), $value, $utf8)
}
try {
    if ((Get-FileHash -LiteralPath $plan.archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $plan.info.sha256) {
        throw 'changed-archive'
    }
    $old = Get-Process -Id $plan.pid -ErrorAction SilentlyContinue
    $armed = @{id=$plan.id; token=$plan.token} | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $plan.transaction 'armed.json.tmp'), $armed, $utf8)
    Move-Item -LiteralPath (Join-Path $plan.transaction 'armed.json.tmp') -Destination (Join-Path $plan.transaction 'armed.json')
    Write-Status 'waiting-for-previous-process'
    if ($old -and -not $old.HasExited) {
        if (-not $old.WaitForExit([int]$plan.parentTimeout * 1000)) { throw 'previous-process-still-running' }
    }
    if (Test-Path -LiteralPath (Join-Path $plan.transaction 'cancel')) { throw 'cancelled' }
    if ((Get-FileHash -LiteralPath $plan.archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $plan.info.sha256) {
        throw 'changed-archive'
    }
    # Exact electron-builder 26 NSIS flags. /D must remain the last argument;
    # NSIS consumes the rest of the command line including spaces as its value.
    # No -Verb RunAs, elevate.exe, --allusers or certificate bypass is used.
    Write-Status 'installing'
    $arguments = '--updated /S --force-run /D=' + $plan.target
    $installer = Start-Process -FilePath $plan.archive -ArgumentList $arguments -PassThru
    if (-not $installer.WaitForExit(600000)) { throw 'installer-still-running' }
    if ($installer.ExitCode -ne 0) { throw ('installer-exit-' + $installer.ExitCode) }
    Write-Status 'waiting-for-new-version'
    $deadline = [DateTime]::UtcNow.AddSeconds([int]$plan.readyTimeout)
    while ([DateTime]::UtcNow -lt $deadline) {
        $receiptPath = Join-Path $plan.transaction 'ready.json'
        if (Test-Path -LiteralPath $receiptPath) {
            $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($receipt.id -eq $plan.id -and $receipt.token -eq $plan.token -and
                $receipt.version -eq $plan.info.version -and $receipt.execPath -eq $plan.execPath) {
                Write-Status 'ready'
                exit 0
            }
        }
        Start-Sleep -Milliseconds 500
    }
    # NSIS owns replacement/recovery on Windows. Do not claim transactional
    # rollback after a completed installer, and never restore/delete user data.
    throw 'new-version-not-ready'
} catch {
    Write-Status ('failed: ' + $_.Exception.Message)
    exit 1
}
