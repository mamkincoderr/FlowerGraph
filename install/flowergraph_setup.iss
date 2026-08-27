; Inno Setup script — FlowerGraph Installer
; Компилируется: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" flowergraph_setup.iss

#define AppName      "FlowerGraph"
#define AppVersion   "0.6.3.2"
#define AppPublisher "FlowerGraph"
#define AppURL       ""
#define AppExeName   "FlowerGraph.exe"
#define DistDir      "..\dist\FlowerGraph"

[Setup]
AppId={{A7C3F2E1-8D4B-4F9A-B2C6-3E5D7F8A9B0C}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=..\..\install
OutputBaseFilename=FlowerGraph_Setup_{#AppVersion}
Compression=lzma/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0.17763
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
; Windows 10 1809 как минимум (поддержка WinRT нужна для PySide6)

[Languages]
Name: "russian";  MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Всё содержимое dist\FlowerGraph\
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\{#AppExeName}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Очищаем AppData при удалении (конфиг программы)
Type: filesandordirs; Name: "{userappdata}\FlowerGraph"

[Code]
// Проверяем наличие Visual C++ Redistributable (нужен для PySide6)
function VCRedistInstalled: Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(HKLM,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
    'Version', Version);
  if not Result then
    Result := RegQueryStringValue(HKLM,
      'SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
      'Version', Version);
end;

procedure InitializeWizard;
begin
  if not VCRedistInstalled then
    MsgBox(
      'Рекомендуется установить Microsoft Visual C++ Redistributable 2015-2022 (x64).' + #13#10 +
      'Скачать можно с сайта Microsoft: aka.ms/vs/17/release/vc_redist.x64.exe',
      mbInformation, MB_OK);
end;
