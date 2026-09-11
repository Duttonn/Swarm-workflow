param([Parameter(ValueFromRemainingArguments=$true)][string[]]$FactoryArgs)
$ErrorActionPreference = 'Stop'
$factoryRoot = $PSScriptRoot
$factoryPaths = @(
    "$factoryRoot\tools\just",
    "$factoryRoot\tools\herdr",
    "$factoryRoot\tools\runtime\node_modules\.bin",
    "$factoryRoot\tools\python-packages\bin",
    "$factoryRoot\.venv\Scripts",
    "$env:LOCALAPPDATA\agy\bin",
    'C:\Program Files\Git\bin',
    'C:\Program Files\Git\usr\bin'
)
$env:PATH = ($factoryPaths -join ';') + ';' + $env:PATH
$env:PYTHONIOENCODING = 'utf-8'
$env:SSSF_DB = "$factoryRoot\adws\adw_data\sssf.db"
$env:SSSF_BLUEPRINTS = "$factoryRoot\blueprints"
$env:PI_PATH = "$factoryRoot\tools\runtime\node_modules\.bin\pi.cmd"
Push-Location $factoryRoot
try {
    if ($FactoryArgs.Count -and $FactoryArgs[0] -eq 'herdr') {
        & "$factoryRoot\tools\herdr\herdr.exe"
    } else {
        & "$factoryRoot\tools\just\just.exe" @FactoryArgs
    }
    exit $LASTEXITCODE
} finally { Pop-Location }
