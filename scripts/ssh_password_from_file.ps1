$ErrorActionPreference = 'Stop'
if (-not $env:SSH_PASSWORD_FILE) {
    exit 1
}
$password = [IO.File]::ReadAllText($env:SSH_PASSWORD_FILE).TrimEnd("`r", "`n")
[Console]::Out.WriteLine($password)
