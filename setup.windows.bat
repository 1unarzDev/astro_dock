@echo off
setlocal enabledelayedexpansion

:: setup.windows.bat [/y]   /y: non-interactive (provisioning): no GitHub login, no git identity prompts, no pause
set "ASTRO_YES="
if /I "%~1"=="/y" set "ASTRO_YES=1"

:: TODO: Need to add automatic VSCode configuration for no port forwarding to allow SITL to work

:: Elevating script to admin
FSUTIL DIRTY query %SystemDrive% >NUL || (
    PowerShell "Start-Process -FilePath cmd.exe -Args '/C CHDIR /D %CD% & ""%0"" %*' -Verb RunAs"
    EXIT
)

echo Checking for Chocolatey...
where choco >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo Installing Chocolatey...
    powershell -NoProfile -InputFormat None -ExecutionPolicy Bypass -Command ^
        "Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))"
    CALL refresh_env.bat
) ELSE (
    echo Chocolatey is already installed.
)

:: Docker Desktop installed outside Chocolatey (its own installer, as the research cluster's join does) is kept:
:: choco would install a second copy over it
set "ASTRO_PKGS=vcxsrv git vscode jq gh nodejs-lts"
if not exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" set "ASTRO_PKGS=%ASTRO_PKGS% docker-desktop"
echo Installing %ASTRO_PKGS% if missing...
choco install -y %ASTRO_PKGS% --ignore-missing
CALL refresh_env.bat

:: WSL 2 is Docker Desktop's backend. A damaged WSL install (Wsl/CallMsi/Install/REGDB_E_CLASSNOTREG) stops at a
:: "press any key to repair" prompt and hangs Docker; updating reinstalls the current package.
echo Updating WSL...
wsl --update --web-download
wsl --set-default-version 2 >nul 2>&1

:: The Dev Container CLI (devcontainer up/exec, as in the README)
where devcontainer >nul 2>&1 || call npm install --global @devcontainers/cli

echo Installing VSCode extensions...

:: Parse the extensions from JSON into a temporary file
IF EXIST ".vscode\extensions.json" (
    jq -r ".recommendations[]" .vscode\extensions.json > extensions.txt

    :: Install extensions listed in file
    for /F "delims=" %%e in (extensions.txt) do (
        if not "%%e"=="" (
            start "" cmd /C code --install-extension %%e --force
        )
    )
) ELSE (
    echo Warning: .vscode\extensions.json not found, skipping extensions installation
)

echo Synchronizing the time...
net start W32Time > NUL 2>&1
w32tm /resync

echo Checking GitHub authentication...
gh auth status >nul 2>&1
if %errorlevel% neq 0 (
    if defined ASTRO_YES (
        echo Not logged in to GitHub; run "gh auth login" later to push.
    ) else (
        echo You are not logged in to GitHub. Starting login...
        gh auth login
    )
) else (
    echo GitHub CLI already authenticated.
)

echo Checking Git user configuration...
for /f "delims=" %%A in ('git config --global user.name 2^>nul') do set GIT_USER_NAME=%%A
for /f "delims=" %%A in ('git config --global user.email 2^>nul') do set GIT_USER_EMAIL=%%A

echo Checking GitHub authentication...
gh auth status >nul 2>&1
if %errorlevel% neq 0 (
    if defined ASTRO_YES (
        echo Not logged in to GitHub; run "gh auth login" later to push.
    ) else (
        echo You are not logged in to GitHub. Starting login...
        gh auth login
    )
) else (
    echo GitHub CLI already authenticated.
)

if not defined GIT_USER_NAME if not defined ASTRO_YES (
    set /p GIT_USER_NAME=Enter your Git username: 
    git config --global user.name "%GIT_USER_NAME%"
) else (
    echo Git user.name already set to "%GIT_USER_NAME%"
)

if not defined GIT_USER_EMAIL if not defined ASTRO_YES (
    set /p GIT_USER_EMAIL=Enter your Git email: 
    git config --global user.email "%GIT_USER_EMAIL%"
) else (
    echo Git user.email already set to "%GIT_USER_EMAIL%"
)

:: Operator config, bind-mounted into the container (the Windows adapter mounts %USERPROFILE%/.config/mhseals)
if not exist "%USERPROFILE%/.config/mhseals" mkdir "%USERPROFILE%/.config/mhseals"

:: Copy docker-compose.override.windows.yml to docker-compose.override.yml
IF EXIST ".\.devcontainer\docker-compose.override.windows.yml" (
    copy /Y ".\.devcontainer\docker-compose.override.windows.yml" ".\.devcontainer\docker-compose.override.yml"
    echo Copied docker-compose.override.windows.yml to docker-compose.override.yml
) ELSE (
    echo Warning: .devcontainer\docker-compose.override.windows.yml not found!
)

:: Detect Docker Desktop install path
set "DOCKER_EXE="
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    set "DOCKER_EXE=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
) else if exist "%LocalAppData%\Programs\Docker\Docker\Docker Desktop.exe" (
    set "DOCKER_EXE=%LocalAppData%\Programs\Docker\Docker\Docker Desktop.exe"
)

if defined DOCKER_EXE (
    echo Docker Desktop found at: "%DOCKER_EXE%"

    :: network_mode: host must reach the Windows host's ports (crane_sim's ROS TCP endpoint on 10000): Docker
    :: Desktop's host networking (4.34+), off by default.
    powershell -NoProfile -ExecutionPolicy Bypass -File ".devcontainer\windows-docker-settings.ps1"
    net localgroup docker-users "%COMPUTERNAME%\%USERNAME%" /add >nul 2>&1

    :: Add Docker Desktop to startup (auto-launch on login)
    echo Adding Docker Desktop to startup...
    reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "Docker Desktop" /d "\"%DOCKER_EXE%\"" /f >nul 2>&1

    :: Start Docker Desktop now
    echo Starting Docker Desktop...
    start "" "%DOCKER_EXE%"

    :: Wait for Docker to initialize (up to 60 seconds)
    echo Waiting for Docker engine to become ready...
    set "docker_ready_flag="
    for /l %%i in (1,1,30) do (
        docker info >nul 2>&1 && (
            set "docker_ready_flag=1"
            echo Docker is running!
            goto :after_docker_check
        )
        timeout /t 2 >nul
    )
    echo Warning: Docker did not start within 60 seconds.
)

:after_docker_check
if not defined DOCKER_EXE (
    echo Warning: Docker Desktop not found in standard locations.
)

echo.
echo Installation complete!
echo You may need to restart your computer.
if not defined ASTRO_YES pause
exit /b