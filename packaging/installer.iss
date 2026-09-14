#ifndef AppVersion
  #error AppVersion must be passed by build.ps1
#endif
[Setup]
AppId={{BB2BD3E5-08A3-42D4-9034-B80CBE163C31}
AppName=Desktop Gallery
AppVersion={#AppVersion}
AppPublisher=yefansky
DefaultDirName={localappdata}\Programs\DesktopGallery
DisableDirPage=yes
DefaultGroupName=Desktop Gallery
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=DesktopGallery-{#AppVersion}-Setup
SetupIconFile=..\app\icon.ico
UninstallDisplayIcon={app}\DesktopGallery.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
AppMutex=Local\NativeWallpaperManagerSingleInstance
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\dist\DesktopGallery\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userdesktop}\Desktop Gallery"; Filename: "{app}\DesktopGallery.exe"
Name: "{userprograms}\Desktop Gallery"; Filename: "{app}\DesktopGallery.exe"

[Run]
Filename: "{app}\DesktopGallery.exe"; Description: "Launch Desktop Gallery"; Flags: nowait postinstall skipifsilent
Filename: "{app}\DesktopGallery.exe"; Flags: nowait runasoriginaluser; Check: ShouldRestartApp

[Code]
function ShouldRestartApp: Boolean;
var
  I: Integer;
begin
  Result := False;
  if not WizardSilent then Exit;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/RESTARTAPP') = 0 then
      Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command: String;
  ExePath: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    ExePath := ExpandConstant('{app}\DesktopGallery.exe');
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
      'NativeWallpaperManager', Command) then
      if (CompareText(Trim(Command), '"' + ExePath + '"') = 0) or
        (CompareText(Trim(Command), ExePath) = 0) then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
          'NativeWallpaperManager');
  end;
end;
