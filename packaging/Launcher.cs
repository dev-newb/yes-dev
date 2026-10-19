using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Yes, Dev")]
[assembly: AssemblyDescription("Yes, Dev portable Windows launcher")]
[assembly: AssemblyVersion("__VERSION__.0")]
[assembly: AssemblyFileVersion("__VERSION__.0")]

internal static class Launcher {
    [STAThread]
    private static int Main(string[] args) {
        try {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            bool check = args.Length == 1 && args[0] == "--check-runtime";
            if (args.Length != 0 && !check)
                throw new ArgumentException("Use YesDev.exe with no arguments, or --check-runtime.");
            string python = Path.Combine(root, "runtime", "pythonw.exe");
            string script = Path.Combine(root, "app", check ? "verify_runtime.py" : "yes_dev.pyw");
            if (!File.Exists(python) || !File.Exists(script))
                throw new FileNotFoundException("Extract the entire ZIP before running YesDev.exe.");
            var start = new ProcessStartInfo(python, "-B \"" + script + "\"");
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            start.WorkingDirectory = Path.Combine(root, "app");
            using (var process = Process.Start(start)) {
                process.WaitForExit();
                if (process.ExitCode != 0 && !check)
                    MessageBox.Show("Yes, Dev did not start. See %LOCALAPPDATA%\\YesDev\\lifecycle and bootstrap-error.log.",
                                    "Yes, Dev", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return process.ExitCode;
            }
        } catch (Exception error) {
            MessageBox.Show(error.Message, "Yes, Dev", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
