; Various Tools: Inno Setup 6 script (plan §4 Phase 8.2).
;
; Per-user install, no admin: %LOCALAPPDATA%\Programs\Various Tools. Start menu shortcut, optional
; desktop shortcut, optional Explorer right-click menu, and an uninstaller that offers to keep the
; settings, history (journal), presets and caches. There is no "start with Windows" option.
;
; Build the app first (packaging\README.md), then compile from the repository root:
;
;   iscc /DAppVersion=0.1.0 packaging\installer.iss
;
; AppVersion must be the version in pyproject.toml; release.yml passes it in. The installer is
; written to dist\Various-Tools-<version>-setup.exe.

#ifndef AppVersion
  #error Pass the version from pyproject.toml: iscc /DAppVersion=X.Y.Z packaging\installer.iss
#endif

#define AppName "Various Tools"
#define AppPublisher "Jay Singhvi"
#define AppURL "https://github.com/jay-singhvi/Empty-Folder-Deleter"
#define GuiExe "Various Tools.exe"
#define CliExe "various-tools.exe"
; The PyInstaller one-dir output (packaging\various_tools.spec).
#define DistDir "..\dist\Various Tools"

; Explorer menu: the same keys and values gui/shell.py writes (Settings > Desktop app >
; shell_integration), so the app and the installer can each add or remove it.
#define MenuKey "Software\Classes\Directory\shell\VariousTools"
#define BackgroundKey "Software\Classes\Directory\Background\shell\VariousTools"

[Setup]
; A fixed AppId keeps upgrades in place. Never change it.
AppId={{6F0B7E52-4C1D-4C8B-9D64-3B8E2F1A7C55}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
UninstallDisplayIcon={app}\{#GuiExe}
UninstallDisplayName={#AppName}
SetupIconFile=icon.ico
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=Various-Tools-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "contextmenu"; Description: "Add ""Open with Various Tools"" to the folder right-click menu"; GroupDescription: "Explorer:"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the whole program folder, so files from an older build never linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#GuiExe}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#GuiExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Only when the task is chosen. uninsdeletekey removes the keys again on uninstall.
Root: HKCU; Subkey: "{#MenuKey}"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Open with Various Tools"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "{#MenuKey}"; ValueType: string; ValueName: "SubCommands"; ValueData: ""; Tasks: contextmenu
Root: HKCU; Subkey: "{#MenuKey}"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#GuiExe}"; Tasks: contextmenu
Root: HKCU; Subkey: "{#MenuKey}\shell\empty_folders"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Find empty folders"; Tasks: contextmenu
Root: HKCU; Subkey: "{#MenuKey}\shell\empty_folders\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#GuiExe}"" --tool empty_folders --folder ""%V"""; Tasks: contextmenu
Root: HKCU; Subkey: "{#MenuKey}\shell\duplicates"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Find duplicates"; Tasks: contextmenu
Root: HKCU; Subkey: "{#MenuKey}\shell\duplicates\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#GuiExe}"" --tool duplicates --folder ""%V"""; Tasks: contextmenu
Root: HKCU; Subkey: "{#BackgroundKey}"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Open with Various Tools"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "{#BackgroundKey}"; ValueType: string; ValueName: "SubCommands"; ValueData: ""; Tasks: contextmenu
Root: HKCU; Subkey: "{#BackgroundKey}"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#GuiExe}"; Tasks: contextmenu
Root: HKCU; Subkey: "{#BackgroundKey}\shell\empty_folders"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Find empty folders"; Tasks: contextmenu
Root: HKCU; Subkey: "{#BackgroundKey}\shell\empty_folders\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#GuiExe}"" --tool empty_folders --folder ""%V"""; Tasks: contextmenu
Root: HKCU; Subkey: "{#BackgroundKey}\shell\duplicates"; ValueType: string; ValueName: "MUIVerb"; ValueData: "Find duplicates"; Tasks: contextmenu
Root: HKCU; Subkey: "{#BackgroundKey}\shell\duplicates\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#GuiExe}"" --tool duplicates --folder ""%V"""; Tasks: contextmenu

[Run]
Filename: "{app}\{#GuiExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent unchecked

[Code]
const
  { platformdirs puts settings, the journal, presets and caches here (appauthor = appname). }
  DataRootName = 'Various Tools';

function DataRoot(): String;
begin
  Result := ExpandConstant('{localappdata}\') + DataRootName;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    { The app can add the Explorer menu itself (Settings). It would point at a removed program,
      so it is always taken out, whether or not the installer added it. }
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, '{#MenuKey}');
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, '{#BackgroundKey}');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    { Keep by default, and always in a silent uninstall: history is what makes Undo possible. }
    if (not UninstallSilent()) and DirExists(DataRoot()) then
    begin
      if MsgBox('Keep your settings, presets, history and caches?' + #13#10 + #13#10 +
                'Choose Yes to keep them for a later install (recommended).' + #13#10 +
                'Choose No to delete ' + DataRoot() + '.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON1) = IDNO then
      begin
        DelTree(DataRoot(), True, True, True);
      end;
    end;
  end;
end;
