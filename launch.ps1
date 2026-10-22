<#
Portable launcher for the powerbi-desktop MCP server.

Claude starts launch.cmd (see .mcp.json), which calls this script to make sure a
private Python environment exists, then runs the server directly (cmd.exe hands
Claude's stdin/stdout straight to Python, which PowerShell cannot do reliably).
This replaces a hard-coded python.exe path, so the plugin works on any Windows PC:

  1. Finds a suitable Python (3.10-3.13, 64-bit): the `py` launcher, PATH, or
     the usual install folders.
  2. If there is none, downloads a private copy of Python 3.12 (python.org's
     "embeddable" zip, SHA256-pinned and signature-checked) into
     %LOCALAPPDATA%\powerbi-desktop-mcp. No admin rights, no PATH changes.
  3. Installs the packages in requirements.txt into a private environment
     (only on first run, or when requirements.txt changes).
  4. Prints the interpreter path (launch.cmd runs the server with it). MCP
     speaks JSON over stdout, so nothing else is ever written there.

Nothing outside %LOCALAPPDATA%\powerbi-desktop-mcp is modified. Setup output
goes to launcher.log in that folder.

Switches:
  -Setup    prepare the environment now (shows progress) and exit (default)
  -Status   show whether/where the environment exists and exit
  -Prepare  (used by launch.cmd) prepare, then print the interpreter path
  -Repair   (used by launch.cmd) after a server crash, check the environment

Environment overrides: POWERBI_MCP_HOME (working folder), POWERBI_MCP_PYTHON
(use this python.exe), POWERBI_MCP_FORCE_EMBED=1 (skip the system-Python
search and use the private downloaded copy).
#>
param([switch]$Setup, [switch]$Status, [switch]$Prepare, [switch]$Repair)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = $PSScriptRoot

$Base = $env:POWERBI_MCP_HOME
if (-not $Base) {
    if ($env:LOCALAPPDATA) { $Base = Join-Path $env:LOCALAPPDATA 'powerbi-desktop-mcp' }
    else { $Base = Join-Path $env:USERPROFILE '.powerbi-desktop-mcp' }
}
New-Item -ItemType Directory -Force -Path $Base | Out-Null
$Base = (Resolve-Path -LiteralPath $Base).Path
$LogFile = Join-Path $Base 'launcher.log'
$EnvDir = Join-Path $Base 'env'
$EmbedDir = Join-Path $Base 'python'
$Marker = Join-Path $Base 'env.ok'
$ReqFile = Join-Path $Root 'requirements.txt'

# python.org "embeddable" build used only when no usable Python is installed.
$EmbedVersion = '3.12.10'
$EmbedUrl = "https://www.python.org/ftp/python/$EmbedVersion/python-$EmbedVersion-embed-amd64.zip"
$EmbedSha256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
$GetPipUrl = 'https://bootstrap.pypa.io/get-pip.py'
$PreferredVersions = @('3.12', '3.13', '3.11', '3.10')   # first = most preferred

# ---------------------------------------------------------------- logging
# Never write to stdout: it is the MCP channel. Use stderr + the log file.
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 512KB)) { Remove-Item $LogFile -Force -ErrorAction SilentlyContinue }
function Say([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
    [Console]::Error.WriteLine("[powerbi-desktop] $Message")
}

function Quote([string]$s) { '"' + $s + '"' }

# Run a program, capture stdout+stderr into the log, return the exit code.
function Invoke-Logged([string]$Exe, [string[]]$Arguments) {
    $out = Join-Path $Base 'step.out.txt'
    $err = Join-Path $Base 'step.err.txt'
    $argString = ($Arguments | ForEach-Object { if ($_ -match '\s') { Quote $_ } else { $_ } }) -join ' '
    $p = Start-Process -FilePath $Exe -ArgumentList $argString -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    foreach ($f in @($out, $err)) {
        if (Test-Path $f) {
            $text = Get-Content $f -Raw -ErrorAction SilentlyContinue
            if ($text) { try { Add-Content -Path $LogFile -Value $text.TrimEnd() -Encoding UTF8 } catch { } }
        }
    }
    return $p.ExitCode
}

# Run a program quickly and return its stdout (or $null on any failure).
function Get-Output([string]$Exe, [string[]]$Arguments) {
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        $psi.Arguments = ($Arguments | ForEach-Object { if ($_ -match '\s') { Quote $_ } else { $_ } }) -join ' '
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        $stdout = $proc.StandardOutput.ReadToEndAsync()
        [void]$proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit(20000)) { try { $proc.Kill() } catch { }; return $null }
        if ($proc.ExitCode -ne 0) { return $null }
        return $stdout.Result
    } catch { return $null }
}

# ---------------------------------------------------------------- finding Python
function Get-PythonInfo([string]$Exe) {
    # -> @{Path; Version; Rank} for a usable interpreter, else $null.
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $null }
    $code = "import sys, venv, ensurepip; print('%d.%d' % sys.version_info[:2], 64 if sys.maxsize > 2**32 else 32)"
    $out = Get-Output $Exe @('-c', $code)
    if (-not $out) { return $null }
    $parts = $out.Trim() -split '\s+'
    if ($parts.Count -lt 2 -or $parts[1] -ne '64') { return $null }
    $rank = [Array]::IndexOf($PreferredVersions, $parts[0])
    if ($rank -lt 0) { return $null }
    return @{ Path = $Exe; Version = $parts[0]; Rank = $rank }
}

function Find-SystemPython {
    $cands = New-Object System.Collections.Generic.List[string]
    if ($env:POWERBI_MCP_PYTHON) { $cands.Add($env:POWERBI_MCP_PYTHON) }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $listing = Get-Output $py.Source @('-0p')
        if ($listing) {
            foreach ($m in [regex]::Matches($listing, '[A-Za-z]:\\[^\r\n]*?python\.exe')) { $cands.Add($m.Value.Trim()) }
        }
    }
    foreach ($name in 'python.exe', 'python3.exe') {
        foreach ($c in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            # Skip the Microsoft Store "App execution alias" stub: running it opens the Store.
            if ($c.Source -and $c.Source -notmatch '\\Microsoft\\WindowsApps\\python[0-9.]*\.exe$') { $cands.Add($c.Source) }
        }
    }
    $globs = @()
    if ($env:LOCALAPPDATA) { $globs += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe') }
    if ($env:ProgramFiles) { $globs += (Join-Path $env:ProgramFiles 'Python3*\python.exe') }
    if (${env:ProgramFiles(x86)}) { $globs += (Join-Path ${env:ProgramFiles(x86)} 'Python3*\python.exe') }
    $globs += 'C:\Python3*\python.exe'
    foreach ($g in $globs) { foreach ($f in @(Get-ChildItem -Path $g -ErrorAction SilentlyContinue)) { $cands.Add($f.FullName) } }

    $best = $null
    $seen = @{}
    foreach ($c in $cands) {
        $key = $c.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        if ($c.StartsWith($EmbedDir, [StringComparison]::OrdinalIgnoreCase)) { continue }
        $info = Get-PythonInfo $c
        if ($info -and (-not $best -or $info.Rank -lt $best.Rank)) { $best = $info }
    }
    return $best
}

# ---------------------------------------------------------------- downloading
function Get-File([string]$Url, [string]$Dest) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try { [System.Net.WebRequest]::DefaultWebProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials } catch { }
    New-Item -ItemType Directory -Force -Path (Split-Path $Dest) | Out-Null
    Say "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
}

function Install-EmbeddedPython {
    $zip = Join-Path $Base 'downloads\python-embed.zip'
    Get-File $EmbedUrl $zip
    $hash = (Get-FileHash -Path $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $EmbedSha256) {
        Remove-Item $zip -Force
        throw "Downloaded Python archive failed its integrity check (SHA256 $hash, expected $EmbedSha256). Not using it."
    }
    if (Test-Path $EmbedDir) { Remove-Item -Recurse -Force $EmbedDir }
    Expand-Archive -Path $zip -DestinationPath $EmbedDir -Force
    $exe = Join-Path $EmbedDir 'python.exe'
    $sig = Get-AuthenticodeSignature -FilePath $exe
    Say "python.exe signature: $($sig.Status) ($($sig.SignerCertificate.Subject))"
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        throw "The downloaded python.exe is not validly signed by the Python Software Foundation. Not using it."
    }
    # The embeddable build ignores site-packages until `import site` is enabled.
    $pth = Get-ChildItem -Path $EmbedDir -Filter 'python*._pth' | Select-Object -First 1
    (Get-Content $pth.FullName) -replace '^\s*#\s*import site', 'import site' | Set-Content -Path $pth.FullName -Encoding ASCII
    $getPip = Join-Path $Base 'downloads\get-pip.py'
    Get-File $GetPipUrl $getPip
    Say 'Installing pip into the private Python'
    if ((Invoke-Logged $exe @($getPip, '--no-warn-script-location', '--disable-pip-version-check')) -ne 0) {
        throw "Installing pip failed. See $LogFile."
    }
    return $exe
}

# ---------------------------------------------------------------- environment
function Test-Environment([string]$Exe) {
    $code = "import sys; sys.path.insert(0, r'$Root'); import mcp, clr, psutil, win32gui, pbi_mcp.server"
    return ((Invoke-Logged $Exe @('-c', $code)) -eq 0)
}

function Test-PackagesPresent([string]$Exe) {
    # Cheap check (no imports, ~0.1s) that every package the server needs is still on disk,
    # so a damaged/partially deleted environment is rebuilt instead of failing later mid-chat.
    $code = "import importlib.util as u; print('ok' if all(u.find_spec(m) for m in ('mcp','clr','clr_loader','psutil','win32gui','pythoncom')) else 'missing')"
    return ((Get-Output $Exe @('-c', $code)) -match '^ok')
}

function Get-WantedMarker {
    $hash = (Get-FileHash -Path $ReqFile -Algorithm SHA256).Hash.ToLowerInvariant()
    return "$hash|1"
}

function Get-EnvPython {
    # Interpreter of an already-prepared environment, or $null.
    if ((Test-Path $Marker) -and ((Get-Content $Marker -Raw).Trim() -eq (Get-WantedMarker))) {
        foreach ($p in @((Join-Path $EnvDir 'Scripts\python.exe'), (Join-Path $EmbedDir 'python.exe'))) {
            if ((Test-Path $p) -and (Test-PackagesPresent $p)) { return $p }
        }
    }
    return $null
}

function Initialize-Environment {
    $ready = Get-EnvPython
    if ($ready) { return $ready }

    $mutex = New-Object System.Threading.Mutex($false, 'Local\powerbi-desktop-mcp-setup')
    $held = $false
    try {
        try { $held = $mutex.WaitOne([TimeSpan]::FromMinutes(15)) } catch [System.Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { throw 'Timed out waiting for another copy of this plugin to finish its first-time setup.' }
        $ready = Get-EnvPython
        if ($ready) { return $ready }

        Say 'First-time setup (or requirements changed): preparing a private Python environment.'
        # A previous setup that was interrupted may have left stray processes/files.
        # Only processes whose *executable* lives inside our private folders are touched.
        $prefixes = @((Join-Path $EnvDir 'x').TrimEnd('x'), (Join-Path $EmbedDir 'x').TrimEnd('x')) | Where-Object { $_.Length -gt ($Base.Length + 1) }
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $exe = $_.ExecutablePath
                $exe -and $_.ProcessId -ne $PID -and $prefixes -and ($prefixes | Where-Object { $exe.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) })
            } |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { } }
        Remove-Item $Marker -Force -ErrorAction SilentlyContinue
        foreach ($d in @($EnvDir, $EmbedDir)) { if (Test-Path $d) { Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue } }

        $exe = $null
        if ($env:POWERBI_MCP_FORCE_EMBED -ne '1') {
            $sys = Find-SystemPython
            if ($sys) {
                Say "Using Python $($sys.Version) at $($sys.Path)"
                if ((Invoke-Logged $sys.Path @('-m', 'venv', $EnvDir)) -eq 0) {
                    $exe = Join-Path $EnvDir 'Scripts\python.exe'
                } else {
                    Say 'Could not create a virtual environment with that Python; falling back to a private copy.'
                }
            } else {
                Say 'No suitable Python (3.10-3.13, 64-bit) found on this PC.'
            }
        }
        if (-not $exe) { $exe = Install-EmbeddedPython }

        Say 'Installing packages (mcp, pythonnet, pywin32, psutil)...'
        $rc = Invoke-Logged $exe @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-warn-script-location', '-q', '-r', $ReqFile)
        if ($rc -ne 0) { throw "Installing the required packages failed (pip exit code $rc). Check your internet/proxy and see $LogFile." }
        if (-not (Test-Environment $exe)) { throw "The environment was created but the plugin can't import its packages. See $LogFile." }
        Set-Content -Path $Marker -Value (Get-WantedMarker) -Encoding ASCII
        Say 'Setup complete.'
        return $exe
    }
    finally {
        if ($held) { try { $mutex.ReleaseMutex() } catch { } }
        $mutex.Dispose()
    }
}

# ---------------------------------------------------------------- main
try {
    if ($Status) {
        $ready = Get-EnvPython
        if ($ready) { Say "Ready. Python: $ready" } else { Say 'Not set up yet. Run launch.ps1 -Setup' }
        Say "Working folder: $Base"
        Say "Plugin folder:  $Root"
        exit 0
    }

    if ($Repair) {
        # Called by launch.cmd after the server exited with an error: if the
        # environment no longer imports cleanly, drop the marker so the next
        # start rebuilds it.
        $ready = Get-EnvPython
        if ($ready -and -not (Test-Environment $ready)) {
            Say 'The environment is damaged; it will be rebuilt on the next start.'
            Remove-Item $Marker -Force -ErrorAction SilentlyContinue
        }
        exit 0
    }

    $python = Initialize-Environment
    if ($Prepare) {
        # launch.cmd reads this single line from stdout: the interpreter to run.
        [Console]::Out.WriteLine($python)
        exit 0
    }
    Say "Ready. Python: $python"
    exit 0
}
catch {
    Say "ERROR: $($_.Exception.Message)"
    exit 1
}
