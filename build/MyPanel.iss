; Установщик «Моей панели» (Inno Setup 6). Собирает его «Собрать установщик.ps1» — версию и пути передаёт он.
#ifndef AppVer
  #define AppVer "1.0.0"
#endif
#ifndef Dist
  #define Dist "dist\MyPanel"
#endif
#ifndef OutDir
  #define OutDir "release"
#endif

[Setup]
AppId={{6E3B9C1A-4F2D-4C8E-9B71-5A0D2E7F3C14}
AppName=Моя панель
AppVersion={#AppVer}
AppVerName=Моя панель {#AppVer}
AppPublisher=Моя панель
DefaultDirName={localappdata}\Programs\Моя панель
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=MyPanel-Setup-{#AppVer}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\MyPanel.exe
UninstallDisplayName=Моя панель
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
MinVersion=10.0.17763

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
ru.WelcomeLabel2=Сейчас на компьютер установится «Моя панель» {#AppVer} — шторка сверху экрана и панель по Alt+Пробел: плеер, перевод, тексты, ссылки, снимки, полка для файлов и заметки.%n%nPython и другие программы ставить не нужно.

[Tasks]
Name: "autostart"; Description: "Запускать вместе с Windows"; GroupDescription: "Дополнительно:"
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
Source: "{#Dist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Моя панель"; Filename: "{app}\MyPanel.exe"; Comment: "Alt+Пробел — открыть панель"
Name: "{userstartup}\Моя панель"; Filename: "{app}\MyPanel.exe"; Tasks: autostart
Name: "{userdesktop}\Моя панель"; Filename: "{app}\MyPanel.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MyPanel.exe"; Description: "Запустить «Мою панель»"; Flags: nowait postinstall skipifsilent
; обновление из самой панели ставится тихо — после него панель запускается сама
Filename: "{app}\MyPanel.exe"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im MyPanel.exe"; Flags: runhidden; RunOnceId: "KillPanel"

[UninstallDelete]
Type: files; Name: "{userstartup}\Моя панель.lnk"
