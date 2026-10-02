; Instalator Asystenta (Inno Setup 6). Budowany w GitHub Actions:
;   iscc /DMyAppVersion=1.2.3 installer\asystent.iss
; Instalacja bez uprawnień administratora do %LOCALAPPDATA%\Programs\Asystent.
; Dane użytkownika (%APPDATA%\Asystent) nie są ruszane ani przy aktualizacji, ani przy odinstalowaniu.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{6F2B1C4E-8D3A-4B7E-9A51-2E7C0D4F9B13}
AppName=Asystent
AppVersion={#MyAppVersion}
AppVerName=Asystent {#MyAppVersion}
AppPublisher=Asystent studenta
AppPublisherURL=https://github.com/Brizuu/asystent-studenta
DefaultDirName={localappdata}\Programs\Asystent
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AsystentSetup
SetupIconFile=..\build\logo.ico
UninstallDisplayIcon={app}\Asystent.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
CloseApplications=force
RestartApplications=no
VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"

[Tasks]
Name: "desktopicon"; Description: "Skrót na pulpicie"; GroupDescription: "Skróty:"

[Files]
Source: "..\dist\Asystent.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\Asystent"; Filename: "{app}\Asystent.exe"
Name: "{userdesktop}\Asystent"; Filename: "{app}\Asystent.exe"; Tasks: desktopicon

[Run]
; bez skipifsilent: po cichej aktualizacji z aplikacji nowa wersja uruchamia się sama.
; Przez cmd z PYINSTALLER_RESET_ENVIRONMENT=1: instalator mógł zostać uruchomiony przez starą wersję i odziedziczyć
; jej zmienne PyInstallera (_MEIPASS2, _PYI_*) — bez resetu nowa wersja szuka DLL w usuniętym folderze _MEI…
Filename: "{cmd}"; Parameters: "/c set PYINSTALLER_RESET_ENVIRONMENT=1&& start """" ""{app}\Asystent.exe"""; Description: "Uruchom Asystenta"; Flags: nowait postinstall runhidden
