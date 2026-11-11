; Inno Setup 安装包脚本（配合 PyInstaller onedir 产物使用）
;
; 用法：
;   1) 先执行打包，产出 dist\fapiao-check\ 文件夹
;   2) 用 Inno Setup 6 编译本脚本：
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\installer.iss
;   3) 产出 dist\发票报销核对助手-Setup-1.2.0.exe
;
; 安装方式：按用户安装（PrivilegesRequired=lowest），无需管理员权限；
; 用户数据不在安装目录，统一写入 %APPDATA%\FapiaoAssistant，卸载不会误删。

#define MyAppName "发票报销核对助手"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "FapiaoAssistant"
#define MyAppExeName "fapiao-check.exe"

[Setup]
AppId={{6F1B2C7A-9D34-4C55-9E86-2A7B1F0C5D11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\FapiaoAssistant
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 按用户安装，避免 UAC 提权与写入 Program Files 的权限问题
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=发票报销核对助手-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; 若已存在 assets\app_icon.ico，可取消下一行注释统一图标
; SetupIconFile=..\assets\app_icon.ico

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; onedir 产物整目录拷贝（含 _internal 运行库）
Source: "..\dist\fapiao-check\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
