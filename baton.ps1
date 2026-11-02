# Run the local checkout without pip installation or global PATH changes.
$batonArguments = @($args)
$batonUserProfile = [Environment]::GetFolderPath('UserProfile')
$batonCandidates = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $batonUserProfile '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe')
)
$batonPython = $batonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $batonPython) {
    $batonPythonCommand = Get-Command python3, python -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike '*\Microsoft\WindowsApps\*' } | Select-Object -First 1
    if ($batonPythonCommand) { $batonPython = $batonPythonCommand.Source }
}
if (-not $batonPython) { throw 'Python 3.10+ was not found. Install Python or use the DSH bundled runtime.' }
$batonPreviousPath = $env:PYTHONPATH
$batonPreviousEncoding = $env:PYTHONIOENCODING
$batonPreviousBytecode = $env:PYTHONDONTWRITEBYTECODE
$batonPreviousConsoleEncoding = [Console]::OutputEncoding
$batonExitCode = 1
try {
    $env:PYTHONPATH = (Join-Path $PSScriptRoot 'src') + [IO.Path]::PathSeparator + $batonPreviousPath
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    if ($batonArguments.Count -eq 0) { $batonArguments = @('--help') }
    & $batonPython -m baton.cli @batonArguments
    $batonExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $batonPreviousPath
    $env:PYTHONIOENCODING = $batonPreviousEncoding
    $env:PYTHONDONTWRITEBYTECODE = $batonPreviousBytecode
    [Console]::OutputEncoding = $batonPreviousConsoleEncoding
}
exit $batonExitCode
