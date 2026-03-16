#define AppName      "Infographic Video Generator"
#define AppVersion   "1.2.0"
#define AppPublisher "Your Name"
#define AppURL       "https://yourwebsite.com"
#define AppExeName   "VideoGeneratorAI.exe"
#define AppIconFile  "app_icon.ico"
#define DistDir      "dist\VideoGeneratorAI"
#define AppDataDir   "{userappdata}\InfographicVideoGenerator"

[Setup]
AppId={{A3F2B841-7C44-4D9E-B123-9F1C2E3D4A5B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=VideoGeneratorAI_Setup_v{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64os
CloseApplications=yes
UninstallDisplayName={#AppName}
SetupIconFile={#AppIconFile}
UninstallDisplayIcon={app}\app_icon.ico
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\app_icon.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}";   Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\app_icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; App install directory leftovers
Type: filesandordirs; Name: "{app}\__pycache__"
; User data written to APPDATA by the running app
Type: files;          Name: "{#AppDataDir}\.env"
Type: files;          Name: "{#AppDataDir}\app.log"
Type: files;          Name: "{#AppDataDir}\video_generator.db"
Type: filesandordirs; Name: "{#AppDataDir}\assets"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDataPath: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    AppDataPath := ExpandConstant('{#AppDataDir}');
    // Remove the APPDATA folder only if it is empty after our targeted deletions above
    if DirExists(AppDataPath) then
      RemoveDir(AppDataPath);
  end;
end;
