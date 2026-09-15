using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Windows.Forms;

public static class ReleaseLauncher
{
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            result.Append('\\', ch == '"' ? slashes * 2 + 1 : slashes);
            slashes = 0;
            result.Append(ch);
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    [STAThread]
    public static int Main(string[] args)
    {
        try
        {
            string directory = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "douyindownload", "_automation");
            var info = new ProcessStartInfo {
                FileName = Path.Combine(directory, "DouyinLiveRecorder.exe"),
                Arguments = string.Join(" ", args.Select(Quote)),
                WorkingDirectory = directory,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            using (Process child = Process.Start(info))
            {
                if (child == null) return 1;
                if (args.Contains("--self-test-report"))
                {
                    if (!child.WaitForExit(90000)) { child.Kill(); return 2; }
                    return child.ExitCode;
                }
                return child.WaitForExit(2500) ? child.ExitCode : 0;
            }
        }
        catch (Exception)
        {
            if (!args.Contains("--self-test-report"))
                MessageBox.Show("无法启动录制程序，请完整解压发布包后重试。", "抖音直播录制");
            return 1;
        }
    }
}
