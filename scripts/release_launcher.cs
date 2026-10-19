using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Windows.Forms;

public static class ReleaseLauncher
{
    private static string StartupError(string executable, Exception error)
    {
        if (!File.Exists(executable))
            return "找不到录制程序：" + executable + "\n请完整解压发布包后重试。";

        var detail = new StringBuilder("无法启动录制程序：")
            .Append(executable)
            .Append("\n\nWindows 报错：")
            .Append(error.Message);
        var windowsError = error as Win32Exception;
        if (windowsError != null)
            detail.Append("\nWindows 错误代码: ").Append(windowsError.NativeErrorCode);
        detail.Append("\n\n如果 Windows 同时提示智能应用控制拦截，请将此错误和被拦截的文件路径告知开发者；用户名等个人目录信息可以打码。");
        return detail.ToString();
    }

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
        string directory = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "douyindownload", "_automation");
        string executable = Path.Combine(directory, "DouyinLiveRecorder.exe");
        try
        {
            var info = new ProcessStartInfo {
                FileName = executable,
                Arguments = string.Join(" ", args.Select(Quote)),
                WorkingDirectory = directory,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            using (Process child = Process.Start(info))
            {
                if (child == null) throw new InvalidOperationException("Windows 未返回录制程序进程。");
                if (args.Contains("--self-test-report"))
                {
                    if (!child.WaitForExit(90000)) { child.Kill(); return 2; }
                    return child.ExitCode;
                }
                return child.WaitForExit(2500) ? child.ExitCode : 0;
            }
        }
        catch (Exception error)
        {
            string message = StartupError(executable, error);
            if (args.Contains("--self-test-report"))
            {
                byte[] diagnostic = new UTF8Encoding(false).GetBytes(message + Environment.NewLine);
                Stream stderr = Console.OpenStandardError();
                stderr.Write(diagnostic, 0, diagnostic.Length);
                stderr.Flush();
            }
            else
                MessageBox.Show(message, "抖音直播录制");
            return 1;
        }
    }
}
