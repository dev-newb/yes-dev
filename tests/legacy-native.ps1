param([string]$SourcePath, [string]$ResultDirectory)
$ErrorActionPreference='Stop'
$provider=$null
$results=New-Object System.Collections.ArrayList
try {
    Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class DesktopName {
 [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] static extern IntPtr GetThreadDesktop(uint id);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr h,int index,StringBuilder text,int len,out int needed);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern bool SetWindowText(IntPtr h,string text);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int mode);
 public static string Current() { var s=new StringBuilder(256); int n; GetUserObjectInformation(GetThreadDesktop(GetCurrentThreadId()),2,s,512,out n); return s.ToString(); }
}
'@
    [IO.File]::WriteAllText((Join-Path $ResultDirectory 'client-desktop.txt'),[DesktopName]::Current())
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile($SourcePath,[ref]$tokens,[ref]$errors)
    if($errors.Count) { throw ($errors | Out-String) }
    # Load declarations only. Do not run watcher startup, its mutex or main loop.
    foreach($stmt in $ast.EndBlock.Statements) {
        if($stmt -is [System.Management.Automation.Language.PipelineAst] -and
           $stmt.PipelineElements[0] -is [System.Management.Automation.Language.CommandAst] -and
           $stmt.PipelineElements[0].GetCommandName() -eq 'Add-Type') {
            . ([scriptblock]::Create($stmt.Extent.Text))
        }
        if($stmt -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
           $stmt.Name -in @('Invoke-Element','Invoke-LegacyElement','Find-DialogWindows','Approve-Dialog','Complete-PendingApprovals')) {
            . ([scriptblock]::Create($stmt.Extent.Text))
        }
    }
    function Write-Log { param($Message,$Level='INFO'); Add-Content -LiteralPath (Join-Path $ResultDirectory 'native.log') -Value "${Level}:$Message" }
    $providerExe=Join-Path $ResultDirectory 'test-controls.exe'
    $stateFile=Join-Path $ResultDirectory 'control-state.txt'
    $stopFile=Join-Path $ResultDirectory 'stop-provider.txt'
    $providerCode=@'
using System;
using System.IO;
using System.Windows.Forms;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows.Automation.Provider;
[ComVisible(true), Guid("e44c3566-915d-4070-99c6-047bff5a08f5"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ILegacyTestProvider {
 void Select(int flags);
 void DoDefaultAction();
 void SetValue([MarshalAs(UnmanagedType.LPWStr)] string value);
 [return: MarshalAs(UnmanagedType.Interface)] object GetIAccessible();
 int ChildId { get; }
 string Name { [return: MarshalAs(UnmanagedType.BStr)] get; }
 string Value { [return: MarshalAs(UnmanagedType.BStr)] get; }
 string Description { [return: MarshalAs(UnmanagedType.BStr)] get; }
 uint Role { get; }
 uint State { get; }
 string Help { [return: MarshalAs(UnmanagedType.BStr)] get; }
 string KeyboardShortcut { [return: MarshalAs(UnmanagedType.BStr)] get; }
 [return: MarshalAs(UnmanagedType.SafeArray, SafeArraySubType=VarEnum.VT_UNKNOWN)] object[] GetSelection();
 string DefaultAction { [return: MarshalAs(UnmanagedType.BStr)] get; }
}
public class TestButton : Button {
 TestProvider provider;
 protected override void WndProc(ref Message m) {
  if(m.Msg==0x3d && m.LParam.ToInt64()==AutomationInteropProvider.RootObjectId) {
   if(provider==null) provider=new TestProvider(this);
   m.Result=AutomationInteropProvider.ReturnRawElementProvider(Handle,m.WParam,m.LParam,provider);
   return;
  }
  base.WndProc(ref m);
 }
}
[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public class TestProvider : IRawElementProviderSimple, IInvokeProvider, ILegacyTestProvider {
 readonly TestButton button; readonly IntPtr hwnd;
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h,System.Text.StringBuilder text,int count);
 public TestProvider(TestButton b) { button=b; hwnd=b.Handle; }
 public ProviderOptions ProviderOptions { get { return ProviderOptions.ServerSideProvider; } }
 public IRawElementProviderSimple HostRawElementProvider { get { return AutomationInteropProvider.HostProviderFromHandle(hwnd); } }
 public object GetPatternProvider(int id) { return id==10000 || id==10018 ? this : null; }
 public object GetPropertyValue(int id) {
  if(id==30003) return 50000;
  if(id==30005) return Name;
  if(id==30002) return System.Diagnostics.Process.GetCurrentProcess().Id;
  if(id==30010 || id==30016 || id==30017) return true;
  return null;
 }
 public void Invoke() { button.BeginInvoke(new Action(()=>button.PerformClick())); }
 public void DoDefaultAction() { Invoke(); }
 public void Select(int flags) { }
 public void SetValue(string value) { throw new NotSupportedException(); }
 public object GetIAccessible() { return null; }
 public int ChildId { get { return 0; } }
 public string Name { get { var text=new System.Text.StringBuilder(256); GetWindowText(hwnd,text,256); return text.ToString(); } }
 public string Value { get { return ""; } }
 public string Description { get { return "Isolated UIA legacy-pattern test control"; } }
 public uint Role { get { return 43; } }
 public uint State { get { return 0; } }
 public string Help { get { return ""; } }
 public string KeyboardShortcut { get { return ""; } }
 public object[] GetSelection() { return null; }
 public string DefaultAction { get { return "Press"; } }
}
public sealed class ProbeForm : Form {
 protected override bool ShowWithoutActivation { get { return true; } }
 public int First { get; private set; }
 public int Second { get; private set; }
 public readonly TestButton A=new TestButton(), B=new TestButton();
 public readonly Label Label=new Label();
 public ProbeForm() {
  Text="YesDev private desktop control test"; ShowInTaskbar=false;
  StartPosition=FormStartPosition.Manual; Location=new Point(-30000,-30000); Size=new Size(400,200);
  A.Text="Allow"; A.Location=new Point(10,10); A.Name="First";
  B.Text="Allow"; B.Location=new Point(120,10); B.Name="Second";
  Label.Text="Allow"; Label.Location=new Point(10,60);
  A.Click+=(s,e)=>{First++;}; B.Click+=(s,e)=>{Second++;};
  Controls.Add(A); Controls.Add(B); Controls.Add(Label);
 }
}
public static class Provider {
 [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] static extern IntPtr GetThreadDesktop(uint id);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr h,int index,System.Text.StringBuilder text,int len,out int needed);
 [STAThread] public static void Main(string[] args) {
  var dn=new System.Text.StringBuilder(256); int needed;
  GetUserObjectInformation(GetThreadDesktop(GetCurrentThreadId()),2,dn,512,out needed);
  File.WriteAllText(args[0]+".desktop",dn.ToString());
  Application.EnableVisualStyles();
  using(var form=new ProbeForm()) using(var timer=new Timer()) {
   timer.Interval=40;
   timer.Tick+=(s,e)=>{
    if(File.Exists(args[1])) { form.Close(); return; }
    try { File.WriteAllText(args[0],String.Join("|",new string[]{
     form.Handle.ToInt64().ToString(), form.A.Handle.ToInt64().ToString(),form.B.Handle.ToInt64().ToString(),
     form.Label.Handle.ToInt64().ToString(),System.Diagnostics.Process.GetCurrentProcess().Id.ToString(),
     form.First.ToString(),form.Second.ToString()})); } catch(IOException) {}
   };
   timer.Start(); Application.Run(form);
  }
 }
}
'@
    Add-Type -AssemblyName UIAutomationProvider
    Add-Type -TypeDefinition $providerCode -ReferencedAssemblies System.Windows.Forms,System.Drawing,([System.Windows.Automation.Provider.IRawElementProviderSimple].Assembly.Location),([System.Windows.Automation.AutomationElement].Assembly.Location),([System.Windows.Automation.Provider.IInvokeProvider].Assembly.Location) -OutputAssembly $providerExe -OutputType WindowsApplication
    $provider=Start-Process -FilePath $providerExe -ArgumentList ('"{0}" "{1}"' -f $stateFile,$stopFile) -WindowStyle Hidden -PassThru
    function Read-State {
        for($i=0;$i -lt 40;$i++) {
            try {
                $parts=[IO.File]::ReadAllText($stateFile).Split('|')
                if($parts.Count -eq 7) { return @($parts | ForEach-Object {[long]$_}) }
            } catch {}
            Start-Sleep -Milliseconds 50
        }
        throw 'Test control state unavailable'
    }
    function Wait-Count($First,$Second) {
        for($i=0;$i -lt 40;$i++) {
            $state=Read-State
            if($state[5] -eq $First -and $state[6] -eq $Second) { return }
            Start-Sleep -Milliseconds 25
        }
        throw "Wrong button counters: first=$($state[5]), second=$($state[6]); expected $First,$Second"
    }
    function Check($Name,[scriptblock]$Body) {
        try { . $Body; [void]$results.Add([pscustomobject]@{name=$Name;passed=$true}) }
        catch { [void]$results.Add([pscustomobject]@{name=$Name;passed=$false;error=$_.Exception.Message}) }
    }
    function Expect($Value,$Expected) { if($Value -ne $Expected) { throw "Expected '$Expected', got '$Value'" } }
    $state=Read-State
    $dialog=[IntPtr]$state[0]
    $first=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$state[1])
    $second=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$state[2])
    $label=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$state[3])
    $providerPid=[int]$state[4]
    $diag=[ordered]@{provider_pid=$providerPid;dialog=$dialog.ToInt64();first_current=($first.Current | Out-String);first_runtime_id=@($first.GetRuntimeId());first_patterns=@($first.GetSupportedPatterns() | ForEach-Object ProgrammaticName)}
    try { $diag.primary_result=$first.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).GetType().FullName } catch { $diag.primary_error=($_ | Out-String) }
    $diag | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'diagnostics.json')
    Check 'Native COM default action on exact button' {
        Expect ([YesDevLegacy]::Invoke($dialog,$first.GetRuntimeId(),$providerPid,'Allow')) $true
        Wait-Count 1 0
    }
    Check 'Duplicate label selects exact runtime ID' {
        Expect ([YesDevLegacy]::Invoke($dialog,$second.GetRuntimeId(),$providerPid,'Allow')) $true
        Wait-Count 1 1
    }
    Check 'Original PowerShell fallback wrapper invokes native control' {
        Expect (Invoke-LegacyElement -Element $first -DialogHwnd $dialog) $true
        Wait-Count 2 1
    }
    Check 'Primary UIA InvokePattern still works' {
        Expect (Invoke-Element -Element $first -DialogHwnd $dialog) 'InvokePattern'
        Wait-Count 3 1
    }
    Check 'Failed primary method reaches real COM fallback' {
        $proxy=[pscustomobject]@{Actual=$second;Current=$second.Current}
        $proxy | Add-Member ScriptMethod GetCurrentPattern { param($Pattern); throw 'Force primary failure for fallback test' }
        $proxy | Add-Member ScriptMethod GetRuntimeId { return ,$this.Actual.GetRuntimeId() }
        Expect (Invoke-Element -Element $proxy -DialogHwnd $dialog) 'LegacyDoDefaultAction'
        Wait-Count 3 2
    }
    Check 'Wrong process rejected' { Expect ([YesDevLegacy]::Invoke($dialog,$first.GetRuntimeId(),($providerPid+1),'Allow')) $false }
    Check 'Changed button name rejected' { Expect ([YesDevLegacy]::Invoke($dialog,$first.GetRuntimeId(),$providerPid,'Cancel')) $false }
    Check 'Non-button control rejected' { Expect ([YesDevLegacy]::Invoke($dialog,$label.GetRuntimeId(),$providerPid,'Allow')) $false }
    Check 'Unknown runtime ID rejected' { Expect ([YesDevLegacy]::Invoke($dialog,[int[]]@(42,987654321),$providerPid,'Allow')) $false }
    Check 'Empty runtime ID rejected' { Expect ([YesDevLegacy]::Invoke($dialog,[int[]]@(),$providerPid,'Allow')) $false }
    Check 'Null dialog rejected' { Expect ([YesDevLegacy]::Invoke([IntPtr]::Zero,$first.GetRuntimeId(),$providerPid,'Allow')) $false }
    Check 'Button cannot replace dialog search boundary' { Expect ([YesDevLegacy]::Invoke([IntPtr]$state[2],$first.GetRuntimeId(),$providerPid,'Allow')) $false }
    Check 'Negative cases did not invoke any button' { Wait-Count 3 2 }
    Check 'Thirty repeated native fallback actions' {
        for($i=0;$i -lt 30;$i++) { Expect ([YesDevLegacy]::Invoke($dialog,$first.GetRuntimeId(),$providerPid,'Allow')) $true }
        Wait-Count 33 2
    }
    $AE=[System.Windows.Automation.AutomationElement]
    $TS=[System.Windows.Automation.TreeScope]
    $CT=[System.Windows.Automation.ControlType]
    $btnCond=New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty,$CT::Button)
    $WindowClass=($AE::FromHandle($dialog)).Current.ClassName
    $DialogPattern=($ast.ParamBlock.Parameters | Where-Object {$_.Name.VariablePath.UserPath -eq 'DialogPattern'}).DefaultValue.SafeGetValue()
    $ApprovePattern=($ast.ParamBlock.Parameters | Where-Object {$_.Name.VariablePath.UserPath -eq 'ApprovePattern'}).DefaultValue.SafeGetValue()
    $normalizer=$ast.EndBlock.Statements | Where-Object {$_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and $_.Left.Extent.Text -eq '$BrowserProcess'} | Select-Object -First 1
    $BrowserProcess=@('test-controls,yesdev-no-such-process')
    . ([scriptblock]::Create($normalizer.Extent.Text))
    $loop=$ast.EndBlock.Statements | Where-Object {$_ -is [System.Management.Automation.Language.WhileStatementAst]}
    $loopText=$loop.Body.Extent.Text.Trim()
    $sweep=[scriptblock]::Create($loopText.Substring(1,$loopText.Length-2))
    $Observe=$false; $parent=$null; $approved=0; $lastSeen=@{}; $procIds=@()
    $pendingApprovals=@{}
    $pidsAt=[datetime]::MinValue; $lastTidy=[datetime]::Now; $procIdsWarned=$false; $IntervalMs=1
    Check 'Real window discovery and process filtering: English' {
        [void][DesktopName]::SetWindowText($dialog,'Allow remote debugging?')
        Expect (Find-DialogWindows).Count 1
        . $sweep
        Expect $approved 0
        Wait-Count 34 2
        [void][DesktopName]::ShowWindow($dialog,0)
        Expect (Complete-PendingApprovals) 1
        Expect (Complete-PendingApprovals) 0
        [void][DesktopName]::ShowWindow($dialog,4)
    }
    Check 'Real Chinese dialog and button approval' {
        $lastSeen=@{}; $approved=0
        $zhTitle=[regex]::Unescape('\u662f\u5426\u5141\u8bb8\u8fdc\u7a0b\u8c03\u8bd5?')
        $zhAllow=[regex]::Unescape('\u5141\u8bb8')
        $zhCancel=[regex]::Unescape('\u53d6\u6d88')
        [void][DesktopName]::SetWindowText($dialog,$zhTitle)
        [void][DesktopName]::SetWindowText([IntPtr]$state[1],$zhAllow)
        [void][DesktopName]::SetWindowText([IntPtr]$state[2],$zhCancel)
        Expect $first.Current.Name $zhAllow
        Expect $second.Current.Name $zhCancel
        Expect (Find-DialogWindows).Count 1
        . $sweep
        Expect $approved 0
        Wait-Count 35 2
        [void][DesktopName]::ShowWindow($dialog,0)
        Expect (Complete-PendingApprovals) 1
        [void][DesktopName]::ShowWindow($dialog,4)
    }
    Check 'Observe mode on real dialog performs no action' {
        $Observe=$true
        Expect (Approve-Dialog -Hwnd $dialog) 'observe'
        Wait-Count 35 2
    }
    Check 'Chrome 154 Chinese title on a real test window' {
        [void][DesktopName]::SetWindowText($dialog,[regex]::Unescape('\u8981\u5141\u8bb8\u8fdc\u7a0b\u8c03\u8bd5\u5417\uff1f'))
        Expect (Find-DialogWindows).Count 1
    }
    Check 'Real process filter rejects unconfigured process' {
        $BrowserProcess=@('yesdev-no-such-process'); $procIds=@(); $pidsAt=[datetime]::MinValue; $lastSeen=@{}; $approved=0
        . $sweep
        Expect $approved 0
        Wait-Count 35 2
    }
    Check 'Real dialog without an approval label is untouched' {
        [void][DesktopName]::SetWindowText([IntPtr]$state[1],'Cancel')
        [void][DesktopName]::SetWindowText([IntPtr]$state[2],'Turn off in settings')
        Expect (Approve-Dialog -Hwnd $dialog) 'nomatch'
        Wait-Count 35 2
    }
    Check 'Real unrelated title is rejected' {
        [void][DesktopName]::SetWindowText($dialog,'Unrelated window')
        Expect (Find-DialogWindows).Count 0
    }
    [IO.File]::WriteAllText($stopFile,'stop')
    if(-not $provider.WaitForExit(3000)) { throw 'Test provider failed to stop' }
    Check 'Closed dialog fails without success' {
        $success=$false
        try { $success=[YesDevLegacy]::Invoke($dialog,[int[]]@(42,987654321),$providerPid,'Allow') } catch {}
        Expect $success $false
    }
} catch {
    [void]$results.Add([pscustomobject]@{name='Native test setup';passed=$false;error=($_ | Out-String)})
} finally {
    if($provider -and -not $provider.HasExited) { Stop-Process -Id $provider.Id -Force }
    $report=[pscustomobject]@{tests=@($results);passed=@($results | Where-Object passed).Count;failed=@($results | Where-Object {-not $_.passed}).Count;desktop=[DesktopName]::Current();isolation='Only test-owned off-screen controls in a separate process are used. No real browser is accessed. No input is sent to the desktop.'}
    $report | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 (Join-Path $ResultDirectory 'native-results.json')
}
if($report.failed) { exit 1 }
