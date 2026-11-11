; Inno Setup 安装包脚本（配合 PyInstaller onedir 产物使用）
;
; 用法：
;   1) 先执行打包，产出 dist\发票报销核对助手\ 文件夹
;   2) 用 Inno Setup 6 编译本脚本：
;        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\installer.iss
;   3) 产出 dist\发票报销核对助手-Setup-1.2.0.exe
;
; 安装方式：以管理员权限安装（PrivilegesRequired=admin），确保能写入安装目录并注册卸载信息；
; 用户数据不在安装目录，统一写入 %APPDATA%\FapiaoAssistant，卸载不会误删。

#define MyAppName "发票报销核对助手"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "峰谷工作室"
#define MyAppExeName "发票报销核对助手.exe"

[Setup]
AppId={{6F1B2C7A-9D34-4C55-9E86-2A7B1F0C5D11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 默认装到 D 盘，避开 C 盘 Program Files 的权限与空间问题
DefaultDirName=D:\FapiaoAssistant
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
; 管理员权限：保证写入安装目录、注册卸载项、写注册表均不失败
PrivilegesRequired=admin
OutputDir=..\dist
OutputBaseFilename=发票报销核对助手-Setup-{#MyAppVersion}
; 改用 zip 压缩：LZMA/LZMA2 的高压缩特征极易被 360/Defender 误判为加壳木马
Compression=zip
SolidCompression=no
; 不在安装前/后强杀进程，避免触发安全软件「程序正在修改其他程序」告警
CloseApplications=no
RestartApplications=no
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; 安装包本身图标（仓库根 assets 下）
SetupIconFile=..\assets\app_icon.ico
; 安装包 exe 的版本资源（右键属性 -> 详细信息可看到）
VersionInfoVersion=1.2.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCopyright=Copyright (C) 2026 {#MyAppPublisher}
; ---------------------------------------------------------------
; 代码签名占位：购买代码签名证书后取消注释即可启用（显著降低 SmartScreen / 杀软误报）
; SignTool=mysigntool
; SignedUninstaller=yes
; ---------------------------------------------------------------

[Messages]
; 欢迎页文案：提前告知「纯本地财务工具」，并给出被安全软件拦截时的操作指引
WelcomeLabel2=这将会在您的电脑上安装【{#MyAppName}】。%n%n⚠️ 重要提示：本软件为纯本地运行的财务工具，绝对安全。若安装过程中您的安全软件（360/电脑管家/Windows Defender）弹出拦截提示，请点击【允许程序所有操作】或【更多信息 → 仍要运行】。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; onedir 产物整目录拷贝（含 _internal 运行库）。
; 注意：用户数据统一在 %APPDATA%\FapiaoAssistant，安装目录不含 data/logs/*.db，
; 因此这里不能使用 Excludes（Inno 的 Excludes 会跨层级匹配，误删 _internal 下同名 data 目录）。
Source: "..\dist\发票报销核对助手\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 不再调用 icacls 修改安装目录 ACL：安装后调用系统工具链修改权限是杀软的高危行为特征，
; 且程序数据统一写在 %APPDATA%\FapiaoAssistant，安装目录只读即可正常运行。
; 如个别机器确实需要写安装目录，可在程序内按需提示，而不是安装时批量提权。
; 完成页勾选「立即运行」；若被杀软隔离，程序会启动失败，此时见安装成功提示
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ 防 C 盘暴雷拦截：禁止安装到 C 盘 Program Files，避免权限不足导致程序异常 }
function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir, UpperDir: string;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Dir := WizardDirValue;
    UpperDir := Uppercase(Dir);
    if (Pos('C:\PROGRAM FILES', UpperDir) = 1) then
    begin
      MsgBox('为防止权限不足导致数据库异常，请勿安装到 C 盘 Program Files 目录。' #13#10
             '推荐保持默认路径 D:\FapiaoAssistant，或选择其他非系统目录。', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ 安装成功后提示防误报：软件无法打开时优先检查杀软隔离区 }
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WizardSilent then
  begin
    MsgBox('安装成功！' + #13#10 + #13#10
           + '如果软件无法打开，请检查是否被安全软件隔离（Windows Defender / 360 / 电脑管家），并加入信任区后重新运行。' + #13#10
           + '软件数据保存在用户 AppData\FapiaoAssistant 目录，卸载不会删除数据。',
           mbInformation, MB_OK);
  end;
end;
