param([string]$ScriptPath, [string]$SourcePath, [string]$ResultDirectory,
      [ValidateRange(10,7200)][int]$TimeoutSeconds=45)
$ErrorActionPreference='Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Diagnostics;
public static class TestDesktop {
 [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
 struct STARTUPINFO {
  public int cb; public string reserved, desktop, title;
  public int x,y,xSize,ySize,xChars,yChars,fill,flags; public short show,reserved2;
  public IntPtr reservedPtr,input,output,error;
 }
 [StructLayout(LayoutKind.Sequential)]
 struct PROCESS_INFORMATION { public IntPtr process,thread; public int pid,tid; }
 [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 static extern IntPtr CreateDesktop(string name, IntPtr device, IntPtr devmode, int flags, uint access, IntPtr security);
 [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desktop);
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 static extern bool CreateProcess(string app, System.Text.StringBuilder command, IntPtr pa, IntPtr ta,
  bool inherit, uint flags, IntPtr environment, string directory, ref STARTUPINFO startup, out PROCESS_INFORMATION info);
 [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint ms);
 [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr handle,out uint code);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
 public static int Run(string exe, string command, string directory, uint timeoutMs) {
  string name="YesDevTest_"+Guid.NewGuid().ToString("N");
  IntPtr desktop=CreateDesktop(name,IntPtr.Zero,IntPtr.Zero,0,0x1ff,IntPtr.Zero);
  if(desktop==IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
  PROCESS_INFORMATION pi=new PROCESS_INFORMATION();
  try {
   STARTUPINFO si=new STARTUPINFO(); si.cb=Marshal.SizeOf(si); si.desktop="winsta0\\"+name;
   if(!CreateProcess(exe,new System.Text.StringBuilder(command),IntPtr.Zero,IntPtr.Zero,false,0x08000000,
      IntPtr.Zero,directory,ref si,out pi)) throw new System.ComponentModel.Win32Exception();
   if(WaitForSingleObject(pi.process,timeoutMs)!=0) {
    var kill=new ProcessStartInfo("taskkill.exe","/PID "+pi.pid+" /T /F");
    kill.UseShellExecute=false; kill.CreateNoWindow=true;
    using(var p=Process.Start(kill)) p.WaitForExit(5000);
    throw new TimeoutException("Owned test desktop process timed out.");
   }
   uint code; GetExitCodeProcess(pi.process,out code); return (int)code;
  } finally {
   if(pi.thread!=IntPtr.Zero) CloseHandle(pi.thread);
   if(pi.process!=IntPtr.Zero) CloseHandle(pi.process);
   CloseDesktop(desktop);
  }
 }
}
'@
$exe = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$scriptAbsolute = (Resolve-Path -LiteralPath $ScriptPath).Path
$sourceAbsolute = (Resolve-Path -LiteralPath $SourcePath).Path
$resultAbsolute = [IO.Path]::GetFullPath($ResultDirectory)
New-Item -ItemType Directory -Path $resultAbsolute -Force | Out-Null
$command = '"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}" -SourcePath "{2}" -ResultDirectory "{3}"' -f $exe,$scriptAbsolute,$sourceAbsolute,$resultAbsolute
$code = [TestDesktop]::Run($exe,$command,(Split-Path -Parent $scriptAbsolute),[uint32]($TimeoutSeconds*1000))
Write-Output "Private desktop test exit code: $code"
exit $code
