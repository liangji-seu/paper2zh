#define ProductName "paper2zh"
#ifndef ProductVersion
  #error ProductVersion must be supplied from VERSION by packaging/build.ps1
#endif

[Setup]
AppName={#ProductName}
AppVersion={#ProductVersion}
DefaultDirName=E:\paper2zh
DefaultGroupName=paper2zh
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=paper2zh-Setup-{#ProductVersion}-win64
SetupIconFile=..\static\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\paper2zh.exe
ChangesAssociations=no

[Files]
Source: "stage\paper2zh\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\paper2zh"; Filename: "{app}\paper2zh.exe"; IconFilename: "{app}\paper2zh.exe"
Name: "{autodesktop}\paper2zh"; Filename: "{app}\paper2zh.exe"; IconFilename: "{app}\paper2zh.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Run]
Filename: "{app}\paper2zh.exe"; Description: "启动 paper2zh"; Flags: nowait postinstall skipifsilent
