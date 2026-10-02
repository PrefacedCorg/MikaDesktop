// NotificationBridge.cs — 用 Windows 原生 WinRT API（底层就是 COM）读取系统通知，并取回完整的通知 XML。
//
// 为什么不用 winsdk：
//   winsdk / winrt 只是 WinRT 的第三方 python 投影，且旧版对 Notification 对象支持不全；
//   直接用系统自带的 WinRT 类型（本文件通过 Windows.winmd 引用编译）不需要任何第三方包。
//
// 数据来源与优先级：
//   1) Windows.UI.Notifications.Management.UserNotificationListener
//      —— 跨应用枚举当前在通知中心里的通知：应用信息(AUMID / 包族名 / 显示名) +
//         每个 binding 的 template / language / hints / 文本元素。
//      —— 这一层 API 本身【不提供】原始 XML（IDL 里只有 Template/Language/Hints/GetTextElements）。
//   2) ToastNotificationManager.History.GetHistoryWithId(aumid)
//      —— 取回该应用通知的原始 XmlDocument，GetXml() 得到的就是完整通知 XML。
//   3) 如果 2) 拿不到（系统未保留历史 / 权限限制 / 该通知不属于历史记录），
//      用 1) 的数据重建一份等价的 toast XML，并在 xmlSource 字段标注 "reconstructed"。
//
// 输出：stdout 每行一个 JSON 对象（NDJSON，UTF-8 无 BOM），便于 python 逐行消费。
// 用法：
//   NotificationBridge.exe request             仅申请一次通知访问权限并打印结果
//   NotificationBridge.exe snapshot            打印当前所有通知（含完整 XML）后退出
//   NotificationBridge.exe watch [intervalMs]  持续监听新通知（默认 800ms 轮询）
//   NotificationBridge.exe remove <tag> <group> <aumid>
//                                              把一条通知从通知中心移除（按钮激活后的收尾）
//   NotificationBridge.exe --help
//
// 编译：同目录 build.ps1（用系统自带 csc.exe + Windows SDK 的 Windows.winmd）。
// 目标框架 C# 5 / .NET Framework 4.x：不要使用 C# 6+ 语法（插值、?.、表达式体成员等）。

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.Threading;
using Windows.ApplicationModel;
using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;
using Windows.UI.Notifications.Management;

namespace NotificationBridge
{
    internal sealed class BindingInfo
    {
        public string Template = "";
        public string Language = "";
        public List<KeyValuePair<string, string>> Hints = new List<KeyValuePair<string, string>>();
        public List<string> Texts = new List<string>();
    }

    internal sealed class HistoryItem
    {
        public string Tag = "";
        public string Group = "";
        public string Xml = "";
        public List<string> Texts = new List<string>();
    }

    internal sealed class HistoryResult
    {
        public List<HistoryItem> Items = new List<HistoryItem>();
        public string Error;
    }

    internal static class Program
    {
        private const string AccessHint =
            "通知访问权限未授予。请打开 设置 → 隐私和安全性 → 通知（旧版为 设置 → 系统 → 通知），" +
            "找到本程序并允许它访问通知，然后重新运行。";

        private static readonly object WriteLock = new object();
        private static readonly HashSet<string> Emitted = new HashSet<string>();

        private static int Main(string[] args)
        {
            try { Console.OutputEncoding = new UTF8Encoding(false); }
            catch { /* 某些宿主不支持改编码，忽略 */ }

            string mode = args.Length > 0 ? args[0].Trim().ToLowerInvariant() : "watch";
            int intervalMs = 800;
            if (args.Length > 1)
            {
                int parsed;
                if (int.TryParse(args[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed) && parsed >= 100)
                {
                    intervalMs = parsed;
                }
            }

            try
            {
                switch (mode)
                {
                    case "request":
                        return CmdRequest();
                    case "snapshot":
                    case "dump":
                        return CmdSnapshot();
                    case "watch":
                        return CmdWatch(intervalMs);
                    case "remove":
                        return CmdRemove(args);
                    case "help":
                    case "-h":
                    case "--help":
                        PrintUsage();
                        return 0;
                    default:
                        Console.Error.WriteLine("未知模式: " + mode);
                        PrintUsage();
                        return 2;
                }
            }
            catch (Exception ex)
            {
                WriteLine(new JsonBuilder()
                    .Add("type", "fatal")
                    .Add("error", ex.GetType().FullName + ": " + ex.Message)
                    .Build());
                Console.Error.WriteLine(AccessHint);
                return 1;
            }
        }

        private static void PrintUsage()
        {
            Console.Error.WriteLine("用法: NotificationBridge.exe [request|snapshot|watch|remove] [参数]");
            Console.Error.WriteLine("  request   仅申请通知访问权限并输出结果");
            Console.Error.WriteLine("  snapshot  输出当前全部通知（含完整 XML）后退出");
            Console.Error.WriteLine("  watch     持续监听新通知，默认每 800ms 轮询一次");
            Console.Error.WriteLine("  remove <tag> <group> <aumid>   把一条通知从通知中心移除");
        }

        /// <summary>把一条通知从通知中心移除（按钮激活之后收尾用）。</summary>
        private static int CmdRemove(string[] args)
        {
            string tag = args.Length > 1 ? args[1] : "";
            string group = args.Length > 2 ? args[2] : "";
            string aumid = args.Length > 3 ? args[3] : "";

            if (string.IsNullOrEmpty(aumid))
            {
                WriteLine(new JsonBuilder()
                    .Add("type", "removed")
                    .AddBool("ok", false)
                    .Add("detail", "缺少 AUMID，无法定位要移除的通知")
                    .Build());
                return 2;
            }

            try
            {
                // ToastNotificationHistory.Remove(tag, group, applicationId)
                ToastNotificationManager.History.Remove(tag, group, aumid);
                WriteLine(new JsonBuilder()
                    .Add("type", "removed")
                    .AddBool("ok", true)
                    .Add("detail", "已从通知中心移除 tag=\"" + tag + "\" group=\"" + group
                                   + "\" aumid=\"" + aumid + "\"")
                    .Build());
                return 0;
            }
            catch (Exception ex)
            {
                WriteLine(new JsonBuilder()
                    .Add("type", "removed")
                    .AddBool("ok", false)
                    .Add("detail", ex.GetType().Name + ": " + ex.Message)
                    .Build());
                return 1;
            }
        }

        private static int CmdRequest()
        {
            UserNotificationListener listener = UserNotificationListener.Current;
            UserNotificationListenerAccessStatus before = listener.GetAccessStatus();
            UserNotificationListenerAccessStatus status = before;
            if (status != UserNotificationListenerAccessStatus.Allowed)
            {
                status = listener.RequestAccessAsync().AsTask().GetAwaiter().GetResult();
            }

            WriteLine(new JsonBuilder()
                .Add("type", "access")
                .Add("before", before.ToString())
                .Add("status", status.ToString())
                .AddBool("allowed", status == UserNotificationListenerAccessStatus.Allowed)
                .Build());

            if (status != UserNotificationListenerAccessStatus.Allowed)
            {
                Console.Error.WriteLine(AccessHint);
                return 3;
            }
            return 0;
        }

        private static int CmdSnapshot()
        {
            UserNotificationListener listener = UserNotificationListener.Current;
            int denied = EnsureAccess(listener);
            if (denied != 0)
            {
                return denied;
            }

            IReadOnlyList<UserNotification> notifs =
                listener.GetNotificationsAsync(NotificationKinds.Toast).AsTask().GetAwaiter().GetResult() as IReadOnlyList<UserNotification>;

            int count = 0;
            if (notifs != null)
            {
                for (int i = 0; i < notifs.Count; i++)
                {
                    UserNotification un = notifs[i];
                    if (un == null)
                    {
                        continue;
                    }
                    try
                    {
                        WriteLine(BuildRecord(un, true));
                        count++;
                    }
                    catch (Exception ex)
                    {
                        WriteLine(new JsonBuilder()
                            .Add("type", "error")
                            .Add("scope", "read")
                            .Add("error", ex.GetType().Name + ": " + ex.Message)
                            .Build());
                    }
                }
            }
            Console.Error.WriteLine("snapshot 完成，共 " + count + " 条通知。");
            return 0;
        }

        private static int CmdWatch(int intervalMs)
        {
            UserNotificationListener listener = UserNotificationListener.Current;
            int denied = EnsureAccess(listener);
            if (denied != 0)
            {
                return denied;
            }

            while (true)
            {
                IReadOnlyList<UserNotification> notifs =
                    listener.GetNotificationsAsync(NotificationKinds.Toast).AsTask().GetAwaiter().GetResult() as IReadOnlyList<UserNotification>;

                HashSet<string> current = new HashSet<string>();
                if (notifs != null)
                {
                    for (int i = 0; i < notifs.Count; i++)
                    {
                        UserNotification un = notifs[i];
                        if (un == null)
                        {
                            continue;
                        }

                        string key;
                        try { key = KeyOf(un); }
                        catch { continue; }

                        current.Add(key);
                        if (!Emitted.Add(key))
                        {
                            continue;
                        }

                        try
                        {
                            WriteLine(BuildRecord(un, false));
                        }
                        catch (Exception ex)
                        {
                            WriteLine(new JsonBuilder()
                                .Add("type", "error")
                                .Add("scope", "read")
                                .Add("notificationKey", key)
                                .Add("error", ex.GetType().Name + ": " + ex.Message)
                                .Build());
                        }
                    }
                }

                // 已经从通知中心消失的条目要移出已读表，否则同 id 的新通知会被漏报。
                Emitted.RemoveWhere(delegate(string k) { return !current.Contains(k); });

                Thread.Sleep(intervalMs);
            }
        }

        /// <summary>0 表示已授权；非 0 表示应该直接返回的退出码。</summary>
        private static int EnsureAccess(UserNotificationListener listener)
        {
            UserNotificationListenerAccessStatus status = listener.GetAccessStatus();
            if (status != UserNotificationListenerAccessStatus.Allowed)
            {
                status = listener.RequestAccessAsync().AsTask().GetAwaiter().GetResult();
            }
            if (status != UserNotificationListenerAccessStatus.Allowed)
            {
                WriteLine(new JsonBuilder()
                    .Add("type", "access")
                    .Add("status", status.ToString())
                    .AddBool("allowed", false)
                    .Build());
                Console.Error.WriteLine(AccessHint);
                return 3;
            }
            return 0;
        }

        private static string KeyOf(UserNotification un)
        {
            string aumid = "";
            try
            {
                AppInfo info = un.AppInfo;
                if (info != null)
                {
                    aumid = info.AppUserModelId;
                }
            }
            catch { /* 个别系统通知取不到 AppInfo，用 id 兜底 */ }
            return (aumid == null ? "" : aumid) + "#" + un.Id.ToString(CultureInfo.InvariantCulture);
        }

        private static string BuildRecord(UserNotification un, bool snapshot)
        {
            string aumid = "";
            string packageFamilyName = "";
            string appName = "";
            string appDescription = "";
            List<string> appErrors = new List<string>();

            try
            {
                AppInfo info = un.AppInfo;
                if (info != null)
                {
                    aumid = info.AppUserModelId;
                    packageFamilyName = info.PackageFamilyName;
                    try
                    {
                        AppDisplayInfo display = info.DisplayInfo;
                        if (display != null)
                        {
                            appName = display.DisplayName;
                            appDescription = display.Description;
                        }
                    }
                    catch (Exception ex)
                    {
                        appErrors.Add("DisplayInfo: " + ex.GetType().Name);
                    }
                }
            }
            catch (Exception ex)
            {
                appErrors.Add("AppInfo: " + ex.GetType().Name + ": " + ex.Message);
            }

            List<BindingInfo> bindings = new List<BindingInfo>();
            List<string> texts = new List<string>();
            List<string> visualErrors = new List<string>();

            try
            {
                Notification notification = un.Notification;
                NotificationVisual visual = notification == null ? null : notification.Visual;
                if (visual != null)
                {
                    foreach (NotificationBinding binding in visual.Bindings)
                    {
                        if (binding == null)
                        {
                            continue;
                        }

                        BindingInfo info = new BindingInfo();
                        try { info.Template = binding.Template; } catch { }
                        try { info.Language = binding.Language; } catch { }
                        try
                        {
                            if (binding.Hints != null)
                            {
                                foreach (KeyValuePair<string, string> kv in binding.Hints)
                                {
                                    info.Hints.Add(kv);
                                }
                            }
                        }
                        catch { }
                        try
                        {
                            foreach (AdaptiveNotificationText element in binding.GetTextElements())
                            {
                                if (element == null)
                                {
                                    continue;
                                }
                                string text = element.Text;
                                info.Texts.Add(text == null ? "" : text);
                                if (!string.IsNullOrEmpty(text))
                                {
                                    texts.Add(text.Trim());
                                }
                            }
                        }
                        catch (Exception ex)
                        {
                            visualErrors.Add("GetTextElements: " + ex.GetType().Name);
                        }

                        bindings.Add(info);
                    }
                }
            }
            catch (Exception ex)
            {
                visualErrors.Add("Visual: " + ex.GetType().Name + ": " + ex.Message);
            }

            // —— 第 2 步：尽力取回原始 XML ——
            HistoryResult history = GetHistory(aumid);
            string xml = null;
            string xmlSource = "none";
            if (history.Items.Count > 0)
            {
                xml = BestMatch(history, texts);
            }
            if (xml != null)
            {
                xmlSource = "history";
            }
            else
            {
                xml = Reconstruct(bindings);
                xmlSource = "reconstructed";
            }

            string title = "";
            string body = "";
            if (bindings.Count > 0)
            {
                List<string> first = bindings[0].Texts;
                for (int i = 0; i < first.Count; i++)
                {
                    if (string.IsNullOrEmpty(first[i]))
                    {
                        continue;
                    }
                    if (title.Length == 0)
                    {
                        title = first[i].Trim();
                    }
                    else if (body.Length == 0)
                    {
                        body = first[i].Trim();
                    }
                }
            }

            JsonBuilder builder = new JsonBuilder();
            builder.Add("type", "notification");
            builder.AddBool("snapshot", snapshot);
            builder.AddNum("id", un.Id);
            builder.Add("aumid", aumid);
            builder.Add("packageFamilyName", packageFamilyName);
            builder.Add("appName", appName);
            builder.Add("appDescription", appDescription);
            builder.Add("creationTime", FormatTime(un));
            builder.Add("title", title);
            builder.Add("body", body);
            builder.Add("xmlSource", xmlSource);
            builder.Add("xml", xml);
            builder.AddNum("historyCount", history.Items.Count);
            builder.Add("historyError", history.Error);

            List<string> bindingJson = new List<string>();
            for (int i = 0; i < bindings.Count; i++)
            {
                BindingInfo info = bindings[i];
                bindingJson.Add(new JsonBuilder()
                    .Add("template", info.Template)
                    .Add("language", info.Language)
                    .AddRaw("hints", Json.StringMap(info.Hints))
                    .AddArray("texts", info.Texts)
                    .Build());
            }
            builder.AddRaw("bindings", Json.RawArray(bindingJson));
            builder.AddArray("texts", texts);
            builder.AddArray("warnings", MergeWarnings(appErrors, visualErrors));

            return builder.Build();
        }

        private static List<string> MergeWarnings(List<string> a, List<string> b)
        {
            List<string> all = new List<string>();
            all.AddRange(a);
            all.AddRange(b);
            return all;
        }

        private static string FormatTime(UserNotification un)
        {
            try
            {
                DateTimeOffset t = un.CreationTime;
                return t.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);
            }
            catch
            {
                return "";
            }
        }

        private static HistoryResult GetHistory(string aumid)
        {
            HistoryResult result = new HistoryResult();
            if (string.IsNullOrEmpty(aumid))
            {
                result.Error = "缺少 AppUserModelId，无法查询该应用的通知历史";
                return result;
            }

            try
            {
                // IDL 里带 [overload("GetHistory")]，所以投影到 C# 是 GetHistory(applicationId) 这个重载。
                IReadOnlyList<ToastNotification> items =
                    ToastNotificationManager.History.GetHistory(aumid) as IReadOnlyList<ToastNotification>;

                if (items == null)
                {
                    return result;
                }

                for (int i = 0; i < items.Count; i++)
                {
                    ToastNotification toast = items[i];
                    if (toast == null)
                    {
                        continue;
                    }

                    HistoryItem item = new HistoryItem();
                    try { item.Tag = toast.Tag; } catch { }
                    try { item.Group = toast.Group; } catch { }
                    try
                    {
                        XmlDocument content = toast.Content;
                        item.Xml = content == null ? null : content.GetXml();
                    }
                    catch (Exception ex)
                    {
                        result.Error = "Content.GetXml: " + ex.GetType().Name + ": " + ex.Message;
                        continue;
                    }

                    if (string.IsNullOrEmpty(item.Xml))
                    {
                        continue;
                    }

                    item.Texts = ExtractTexts(item.Xml);
                    result.Items.Add(item);
                }
            }
            catch (Exception ex)
            {
                result.Error = ex.GetType().Name + ": " + ex.Message;
            }

            return result;
        }

        private static List<string> ExtractTexts(string xml)
        {
            List<string> texts = new List<string>();
            try
            {
                // 用 BCL 的 System.Xml 解析字符串即可：目的只是取出 <text> 的文本用于和历史条目配对。
                System.Xml.XmlDocument doc = new System.Xml.XmlDocument();
                doc.LoadXml(xml);
                System.Xml.XmlNodeList nodes = doc.GetElementsByTagName("text");
                for (int i = 0; i < nodes.Count; i++)
                {
                    System.Xml.XmlNode node = nodes.Item(i);
                    if (node == null)
                    {
                        continue;
                    }
                    string inner = node.InnerText;
                    if (!string.IsNullOrEmpty(inner))
                    {
                        texts.Add(inner.Trim());
                    }
                }
            }
            catch { /* 解析失败就用空文本列表，后面还有单条兜底逻辑 */ }
            return texts;
        }

        private static string BestMatch(HistoryResult history, List<string> texts)
        {
            if (history == null || history.Items.Count == 0)
            {
                return null;
            }

            HashSet<string> want = new HashSet<string>();
            for (int i = 0; i < texts.Count; i++)
            {
                string t = Normalize(texts[i]);
                if (t.Length > 0)
                {
                    want.Add(t);
                }
            }

            string best = null;
            int bestScore = 0;
            for (int i = 0; i < history.Items.Count; i++)
            {
                HistoryItem item = history.Items[i];
                int score = 0;
                for (int j = 0; j < item.Texts.Count; j++)
                {
                    if (want.Contains(Normalize(item.Texts[j])))
                    {
                        score++;
                    }
                }
                if (score > bestScore)
                {
                    bestScore = score;
                    best = item.Xml;
                }
            }

            if (bestScore > 0)
            {
                return best;
            }

            // 文本完全对不上时：该应用历史里只有一条，就直接用；否则宁可重建也不猜。
            return history.Items.Count == 1 ? history.Items[0].Xml : null;
        }

        private static string Normalize(string s)
        {
            return s == null ? "" : s.Trim().ToLowerInvariant();
        }

        private static string Reconstruct(List<BindingInfo> bindings)
        {
            StringBuilder sb = new StringBuilder();
            sb.Append("<toast><visual>");
            for (int i = 0; i < bindings.Count; i++)
            {
                BindingInfo info = bindings[i];
                sb.Append("<binding");
                if (!string.IsNullOrEmpty(info.Template))
                {
                    sb.Append(" template=\"").Append(XmlEscape(info.Template)).Append('"');
                }
                if (!string.IsNullOrEmpty(info.Language))
                {
                    sb.Append(" lang=\"").Append(XmlEscape(info.Language)).Append('"');
                }
                for (int h = 0; h < info.Hints.Count; h++)
                {
                    sb.Append(" hint-")
                      .Append(XmlEscape(info.Hints[h].Key))
                      .Append("=\"")
                      .Append(XmlEscape(info.Hints[h].Value))
                      .Append('"');
                }
                sb.Append('>');
                for (int t = 0; t < info.Texts.Count; t++)
                {
                    sb.Append("<text>").Append(XmlEscape(info.Texts[t])).Append("</text>");
                }
                sb.Append("</binding>");
            }
            sb.Append("</visual></toast>");
            return sb.ToString();
        }

        private static string XmlEscape(string s)
        {
            if (string.IsNullOrEmpty(s))
            {
                return "";
            }
            StringBuilder sb = new StringBuilder(s.Length);
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                switch (c)
                {
                    case '&': sb.Append("&amp;"); break;
                    case '<': sb.Append("&lt;"); break;
                    case '>': sb.Append("&gt;"); break;
                    case '"': sb.Append("&quot;"); break;
                    case '\'': sb.Append("&apos;"); break;
                    default: sb.Append(c); break;
                }
            }
            return sb.ToString();
        }

        private static void WriteLine(string line)
        {
            lock (WriteLock)
            {
                Console.Out.Write(line);
                Console.Out.Write('\n');
                Console.Out.Flush();
            }
        }
    }

    /// <summary>最小 JSON 构造器，避免依赖 System.Web.Extensions 之类的额外程序集。</summary>
    internal sealed class JsonBuilder
    {
        private readonly StringBuilder _sb = new StringBuilder("{");
        private bool _first = true;

        public JsonBuilder Add(string key, string value)
        {
            return AddRaw(key, value == null ? "null" : Json.Quote(value));
        }

        public JsonBuilder AddRaw(string key, string rawJson)
        {
            Separate();
            _sb.Append(Json.Quote(key)).Append(':').Append(rawJson == null ? "null" : rawJson);
            return this;
        }

        public JsonBuilder AddNum(string key, long value)
        {
            Separate();
            _sb.Append(Json.Quote(key)).Append(':').Append(value.ToString(CultureInfo.InvariantCulture));
            return this;
        }

        public JsonBuilder AddBool(string key, bool value)
        {
            Separate();
            _sb.Append(Json.Quote(key)).Append(':').Append(value ? "true" : "false");
            return this;
        }

        public JsonBuilder AddArray(string key, List<string> values)
        {
            Separate();
            _sb.Append(Json.Quote(key)).Append(':').Append(Json.StringArray(values));
            return this;
        }

        public string Build()
        {
            return _sb.ToString() + "}";
        }

        private void Separate()
        {
            if (_first)
            {
                _first = false;
            }
            else
            {
                _sb.Append(',');
            }
        }
    }

    internal static class Json
    {
        public static string Quote(string s)
        {
            if (s == null)
            {
                return "null";
            }
            StringBuilder sb = new StringBuilder(s.Length + 2);
            sb.Append('"');
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < ' ')
                        {
                            sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            sb.Append(c);
                        }
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }

        public static string StringArray(List<string> values)
        {
            StringBuilder sb = new StringBuilder("[");
            if (values != null)
            {
                for (int i = 0; i < values.Count; i++)
                {
                    if (i > 0)
                    {
                        sb.Append(',');
                    }
                    sb.Append(Quote(values[i]));
                }
            }
            return sb.Append(']').ToString();
        }

        public static string RawArray(List<string> rawJsonItems)
        {
            StringBuilder sb = new StringBuilder("[");
            if (rawJsonItems != null)
            {
                for (int i = 0; i < rawJsonItems.Count; i++)
                {
                    if (i > 0)
                    {
                        sb.Append(',');
                    }
                    sb.Append(rawJsonItems[i]);
                }
            }
            return sb.Append(']').ToString();
        }

        public static string StringMap(List<KeyValuePair<string, string>> pairs)
        {
            StringBuilder sb = new StringBuilder("{");
            if (pairs != null)
            {
                for (int i = 0; i < pairs.Count; i++)
                {
                    if (i > 0)
                    {
                        sb.Append(',');
                    }
                    sb.Append(Quote(pairs[i].Key)).Append(':').Append(Quote(pairs[i].Value));
                }
            }
            return sb.Append('}').ToString();
        }
    }
}
