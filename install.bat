@echo off
REM ════════════════════════════════════════
REM   TTS Studio — Einrichtung (Windows)
REM ════════════════════════════════════════
REM Doppelklick auf diese Datei richtet alles ein.

cd /d "%~dp0"
echo ========================================
echo   TTS Studio - Einrichtung (Windows)
echo ========================================

REM 1. Python pruefen
where python >nul 2>&1
if errorlevel 1 (
    echo [FEHLER] Python nicht gefunden.
    echo Bitte Python von https://python.org/downloads installieren
    echo und dabei "Add Python to PATH" ankreuzen. Dann diese Datei erneut ausfuehren.
    pause
    exit /b 1
)
echo [OK] Python gefunden
python --version

REM 2. ffmpeg pruefen, ggf. per winget installieren
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [!] ffmpeg nicht gefunden - versuche Installation per winget...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    echo.
    echo [!] WICHTIG: Bitte dieses Fenster schliessen, ein NEUES Terminal oeffnen
    echo     und install.bat erneut ausfuehren, damit ffmpeg im PATH landet.
    pause
    exit /b 0
)
echo [OK] ffmpeg gefunden

REM 2b. git pruefen, ggf. per winget installieren
where git >nul 2>&1
if errorlevel 1 (
    echo [!] git nicht gefunden - versuche Installation per winget...
    winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
    echo.
    echo [!] WICHTIG: Bitte dieses Fenster schliessen, ein NEUES Terminal oeffnen
    echo     und install.bat erneut ausfuehren, damit git im PATH landet.
    pause
    exit /b 0
)
echo [OK] git gefunden

REM 3. venv + Pakete
if not exist "venv" (
    echo -^> Virtuelle Umgebung anlegen...
    python -m venv venv
)
echo -^> Pakete installieren ^(PyTorch/Whisper ~2GB, kann dauern^)...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

REM 4. .env vorbereiten
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo -^> .env aus Vorlage erstellt - bitte Keys eintragen!
    )
)

echo.
echo ========================================
echo   Fertig!
echo ========================================
echo   Naechste Schritte:
echo   1. .env oeffnen und API-Keys + Spreadsheet-ID eintragen
echo   2. service_account.json in diesen Ordner legen
echo   3. TTS_Studio.bat per Doppelklick starten
echo      ^(Verknuepfung: Rechtsklick -^> Senden an -^> Desktop^)
echo ========================================
pause