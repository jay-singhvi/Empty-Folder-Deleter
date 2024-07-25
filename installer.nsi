; Empty Folder Deleter Installer Script

!include "MUI2.nsh"
!include "LogicLib.nsh"

; Define your application name
!define APPNAME "Empty Folder Deleter"
!define COMPANYNAME "Jay Singhvi"
!define DESCRIPTION "A tool for deleting empty folders"

; Define application version
!define VERSIONMAJOR 1
!define VERSIONMINOR 0
!define VERSIONBUILD 0

; Main Install settings
Name "${APPNAME}"
InstallDir "$PROGRAMFILES\${COMPANYNAME}\${APPNAME}"
InstallDirRegKey HKLM "Software\${COMPANYNAME}\${APPNAME}" "Install_Dir"
RequestExecutionLevel admin
OutFile "EmptyFolderDeleterSetup.exe"

; Modern interface settings
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\EmptyFolderDeleter.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Run ${APPNAME} now"
!define MUI_FINISHPAGE_RUN_NOTCHECKED

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

; Add a custom page for additional options
Page custom AdditionalOptionsPage AdditionalOptionsLeave

!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Set languages (first is default language)
!insertmacro MUI_LANGUAGE "English"

; Vars for checkboxes
Var StartupShortcut

; Custom page for additional options
Function AdditionalOptionsPage
  !insertmacro MUI_HEADER_TEXT "Additional Options" "Choose additional settings for ${APPNAME}"
  nsDialogs::Create 1018
  Pop $0

  ${NSD_CreateCheckbox} 10 10 100% 12u "Start ${APPNAME} on Windows startup"
  Pop $StartupShortcut

  nsDialogs::Show
FunctionEnd

Function AdditionalOptionsLeave
  ${NSD_GetState} $StartupShortcut $0
FunctionEnd

Section "Install"
  SetOutPath $INSTDIR
  File "dist\EmptyFolderDeleter.exe"

  ; Write uninstall information to the registry
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" "DisplayName" "${COMPANYNAME} - ${APPNAME} - ${DESCRIPTION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" "QuietUninstallString" "$\"$INSTDIR\uninstall.exe$\" /S"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" "InstallLocation" "$\"$INSTDIR$\""
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" "Publisher" "$\"${COMPANYNAME}$\""
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" "DisplayVersion" "$\"${VERSIONMAJOR}.${VERSIONMINOR}.${VERSIONBUILD}$\""

  ; Create uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Create start menu shortcut
  CreateDirectory "$SMPROGRAMS\${COMPANYNAME}"
  CreateShortcut "$SMPROGRAMS\${COMPANYNAME}\${APPNAME}.lnk" "$INSTDIR\EmptyFolderDeleter.exe"

  ; Create startup shortcut if selected
  ${If} $0 == 1
    CreateShortcut "$SMSTARTUP\${APPNAME}.lnk" "$INSTDIR\EmptyFolderDeleter.exe"
  ${EndIf}
SectionEnd

Section "Uninstall"
  ; Remove Start Menu launcher
  Delete "$SMPROGRAMS\${COMPANYNAME}\${APPNAME}.lnk"
  RMDir "$SMPROGRAMS\${COMPANYNAME}"

  ; Remove startup shortcut
  Delete "$SMSTARTUP\${APPNAME}.lnk"

  ; Remove files
  Delete $INSTDIR\EmptyFolderDeleter.exe
  Delete $INSTDIR\Uninstall.exe

  ; Remove uninstaller information from the registry
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}"

  ; Remove installation directory
  RMDir "$INSTDIR"
SectionEnd
