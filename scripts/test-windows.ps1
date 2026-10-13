param([switch]$Ui)
$ErrorActionPreference = 'Stop'

# Temporary audit branch only: keep the existing WMI/Cargo Job escape, then run
# the console diagnostic alone. The original Windows suite passed in the prior run.
# -Ui remains accepted because the existing WMI host supplies it.
$artifacts = @(cargo test -p zeroshot --lib --no-run --message-format=json | ForEach-Object {
    $_ | ConvertFrom-Json
})
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$tests = @($artifacts | Where-Object {
    $_.reason -eq 'compiler-artifact' -and $_.profile.test -and $_.executable
})
if ($tests.Count -ne 1) { throw 'Cargo did not produce exactly one diagnostic test executable' }

$test = $tests[0]
Push-Location (Split-Path -Parent $test.manifest_path)
try {
    & $test.executable --exact execution::platform::windows::controller::tests::console_probe_tests::owned_job_and_break0_diagnostic --nocapture
    $status = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $status
