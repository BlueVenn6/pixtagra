; NSIS is permitted for commercial use. The payload has already passed runtime
; verification; this installer never rebuilds or downloads application code.
Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma
!include "MUI2.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\PixTagra"
Name "PixTagra"
OutFile "${InstallerOutput}"
InstallDir "$LOCALAPPDATA\Programs\PixTagra"
VIProductVersion "${ReleaseVersion}"
VIAddVersionKey "ProductName" "PixTagra"
VIAddVersionKey "CompanyName" "Nera Venn"
VIAddVersionKey "FileDescription" "PixTagra installer (${SourceCommit})"
VIAddVersionKey "FileVersion" "${ReleaseVersion}"
VIAddVersionKey "LegalCopyright" "Nera Venn, MIT License"
ShowInstDetails show
ShowUninstDetails show
!define MUI_ABORTWARNING
!define MUI_ICON "${Payload}\_internal\assets\branding\PixTagra_App_Icon.ico"
!define MUI_UNICON "${Payload}\_internal\assets\branding\PixTagra_App_Icon.ico"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${LicenseFile}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "PixTagra requires 64-bit Windows."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "PixTagra requires Windows 10 or later."
    Abort
  ${EndIf}
  SetRegView 64
  SetShellVarContext current
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}
FunctionEnd

Section "PixTagra" Main
  ; Opening with write access detects a running executable without truncating it.
  IfFileExists "$INSTDIR\PixTagra.exe" 0 ready
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\PixTagra.exe", i 0x40000000, i 1, p 0, i 3, i 0, p 0) p.r0'
  ${If} $0 == -1
    MessageBox MB_OK|MB_ICONSTOP "Close PixTagra and check that the installation folder is writable, then run setup again."
    SetErrorLevel 1
    Abort
  ${EndIf}
  System::Call 'kernel32::CloseHandle(p r0)'
  ready:
  SetOutPath "$INSTDIR"
  SetOverwrite on
  ClearErrors
  File /r "${Payload}\*"
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONSTOP "Installation could not copy all required files. Please run setup again."
    SetErrorLevel 1
    Abort
  ${EndIf}
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\PixTagra"
  CreateShortcut "$SMPROGRAMS\PixTagra\PixTagra.lnk" "$INSTDIR\PixTagra.exe" "" "$INSTDIR\PixTagra.exe" 0
  CreateShortcut "$SMPROGRAMS\PixTagra\Uninstall PixTagra.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\Uninstall.exe" 0
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "PixTagra"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${ReleaseVersion}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Nera Venn"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '$\"$INSTDIR\Uninstall.exe$\" /S'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\PixTagra.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "SourceCommit" "${SourceCommit}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "HelpLink" "mailto:neravenn@gmail.com"
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetRegView 64
  SetShellVarContext current
  !include "${UninstallFiles}"
  Delete "$INSTDIR\Uninstall.exe"
  ; RMDir has no /r: images or any other files the user added are preserved.
  RMDir "$INSTDIR"
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
  ${If} $0 == $INSTDIR
    Delete "$SMPROGRAMS\PixTagra\PixTagra.lnk"
    Delete "$SMPROGRAMS\PixTagra\Uninstall PixTagra.lnk"
    RMDir "$SMPROGRAMS\PixTagra"
    DeleteRegKey HKCU "${UNINSTALL_KEY}"
  ${EndIf}
SectionEnd
