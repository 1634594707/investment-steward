package com.investment.steward.mobile;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ContentValues;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.ssl.HttpsURLConnection;

/**
 * 轻量移动端壳(§5 阶段 5 · 四页架构 v0.3.0):
 * - WebView 加载内置 SPA(assets/www/index.html):总览/自选股/研究工作台/设置;
 * - 所有网络请求经原生桥:域名白名单(relay + 行情公开端点 + 内置AI + 自定义AI),AI key 只存在于原生层;
 * - 后台 AI 任务:startAiTask 提交后原生线程跑 /v1/chat/completions,完成发系统通知 + 回调 __aiTaskDone;
 * - 手机不读 Windows SQLite,数据全部来自同步白名单(relay 端到端密文)。
 */
public class MainActivity extends Activity {
    // E1(frontend-optimization-roadmap-2026-09-12)：内置免费AI渠道下线，静态 Key 从源码移除
    // （硬编码在 APK 内可被反编译提取，属密钥泄漏）。AI 一律走用户自配的 OpenAI 兼容通道
    // （host 进白名单、key 由原生注入）。遗留事项：已泄漏的 Key 需在服务提供方轮换。
    /** 基础白名单:relay + 公开行情端点(腾讯K线 / 东财K线兜底·榜单·公告·新闻·财报 / 新浪榜单)。 */
    private static final String[] BASE_HOSTS = {
            "st.18257.xyz",
            "web.ifzq.gtimg.cn",
            "push2his.eastmoney.com",
            "push2.eastmoney.com",
            "np-weblist.eastmoney.com",
            "push2delay.eastmoney.com",
            "np-anotice-stock.eastmoney.com",
            "search-api-web.eastmoney.com",
            "datacenter-web.eastmoney.com",
            "vip.stock.finance.sina.com.cn"
    };
    /** 壳版本(与 manifest versionName 同步改)。 */
    private static final String APP_VERSION = "0.3.6";
    private static final int NOTIFY_ID = 20260910;
    private static final String CHANNEL_ID = "aitask";

    /** 用户在设置页填的自定义 AI 通道(OpenAI 兼容):host 进白名单、key 由原生注入 Authorization。 */
    private volatile String customAiHost = null;
    private volatile String customAiKey = null;

    /** 后台 AI 任务表:taskId → 状态机。 */
    private static class AiTask {
        volatile String status = "running"; // running | done | error
        volatile String raw = "";          // done 时的原始 HTTP 响应体(JSON),由 JS 解析
        volatile String err = "";
    }
    private static final ConcurrentHashMap<String, AiTask> TASKS = new ConcurrentHashMap<>();

    private WebView web;
    private final ExecutorService pool = Executors.newFixedThreadPool(2);
    /** AI 后台任务专用单线程执行器:多标的队列严格串行,互不抢并发。 */
    private final ExecutorService aiPool = Executors.newSingleThreadExecutor();

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setTheme(getResources().getIdentifier("AppTheme", "style", getPackageName()));
        // API 33+ 通知运行时权限(后台研报完成提醒)
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1);
        }
        web = new WebView(this);
        WebView.setWebContentsDebuggingEnabled(true); // chrome://inspect 远程排障
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        // android_asset 主帧加载兜底:部分 ROM 对 file 访问默认收紧,显式放开(仅本地资产)
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(false);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                return false; // 全部内加载,SPA 不外跳
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, android.webkit.WebResourceError err) {
                if (req.isForMainFrame()) showDiagnostic("load error: " + err.getDescription());
            }
        });
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        web.setBackgroundColor(0xFF0B1018);
        // ★ 关键修复:退出系统「深色模式强制加深」。ROM 的 Force Dark 会把已暗色主题的
        //   浅色文字再压暗 → 暗底暗字 = 全黑屏(报错页白底黑字被反转后反而可见,与现象完全吻合)。
        if (Build.VERSION.SDK_INT >= 33) {
            s.setAlgorithmicDarkeningAllowed(false);   // API 33+
        }
        if (Build.VERSION.SDK_INT >= 29) {
            s.setForceDark(WebSettings.FORCE_DARK_OFF); // API 29~32
        }
        if (Build.VERSION.SDK_INT >= 30) {
            web.setForceDarkAllowed(false);             // View 层(API 30+),覆盖 MIUI 等私有加深
        }
        setContentView(web);
        // 版本号 Toast 不依赖 WebView 渲染,任何情况都能证明手机跑的是哪个包
        android.widget.Toast.makeText(this, "研投管家 v" + APP_VERSION, android.widget.Toast.LENGTH_LONG).show();
        // 启动采集(无论走哪条路都记录下来,渲染失败时在页面/Toast 上可见)
        StringBuilder boot = new StringBuilder();
        // ① 优先读包内资产(正常路径)
        String html = readAsset("www/index.html", boot);
        boolean fromEmbedded = false;
        // ② 资产不可见时,用构建期内嵌的 base64 快照兜底(绕开任何 AssetManager 怪癖)
        if (html == null || html.trim().isEmpty()) {
            html = EmbeddedPage.html();
            fromEmbedded = true;
            boot.append("fallback=embedded(EmbeddedPage) ok=").append(html != null).append("\n");
        }
        if (html == null || html.trim().isEmpty()) {
            showDiagnostic(boot.toString());
            return;
        }
        // 用 loadDataWithBaseURL(非 loadData):避免 data: URL 遇 '#' 截断导致空页
        web.loadDataWithBaseURL("https://appassets.investment-steward.local/", html, "text/html", "utf-8", null);
        android.widget.Toast.makeText(this, "v" + APP_VERSION + (fromEmbedded ? " · 内嵌页" : " · 资产页"),
                android.widget.Toast.LENGTH_SHORT).show();
    }

    private String readAsset(String path, StringBuilder boot) {
        try {
            String[] top = getAssets().list("");
            boot.append("assets('')=").append(java.util.Arrays.toString(top)).append("\n");
        } catch (Exception e) { boot.append("assets('') err: ").append(e).append("\n"); }
        try {
            InputStream is = getAssets().open(path);
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) bo.write(buf, 0, n);
            is.close();
            boot.append("open ").append(path).append(" ok bytes=").append(bo.size()).append("\n");
            return new String(bo.toByteArray(), StandardCharsets.UTF_8);
        } catch (Exception e) {
            boot.append("open ").append(path).append(" err: ").append(e).append("\n");
            return null;
        }
    }

    /** 诊断页(data: URL,暗色):显示壳版本/包名/首次安装时间,用于识别"手机装的不是最新包"。 */
    private void showDiagnostic(String reason) {
        String detail;
        try {
            long firstInstall = getPackageManager()
                    .getPackageInfo(getPackageName(), 0).firstInstallTime;
            detail = "version=" + APP_VERSION
                    + " | pkg=" + getPackageName()
                    + " | firstInstall=" + new java.util.Date(firstInstall)
                    + " | " + reason;
        } catch (Exception e) {
            detail = "version=" + APP_VERSION + " | " + reason;
        }
        String html = "<html><head><title>diag</title></head><body style='background:#fff;color:#000;font-family:monospace;padding:20px'>"
                + "<h3 style='color:#c47f00'>研投管家 · 启动诊断 v" + APP_VERSION + "</h3>"
                + "<p style='white-space:pre-wrap;word-break:break-all;font-size:12px'>" + detail + "</p>"
                + "<p style='color:#666;font-size:12px'>资产与内嵌快照均不可用,请把本屏截图发回。</p>"
                + "</body></html>";
        web.loadDataWithBaseURL("https://diag.investment-steward.local/", html, "text/html", "utf-8", null);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) web.destroy();
        super.onDestroy();
    }

    static boolean hostAllowed(String url) {
        try {
            String host = new URL(url).getHost();
            if (host == null) return false;
            for (String ok : BASE_HOSTS) if (host.equals(ok)) return true;
            return host.equals(customAiHostStatic());
        } catch (Exception e) {
            return false;
        }
    }

    private static String customAiHostStatic() {
        return CUSTOM_HOST.get();
    }

    /** 静态镜像:JavascriptInterface 的静态方法也需要读自定义 host。 */
    private static final java.util.concurrent.atomic.AtomicReference<String> CUSTOM_HOST =
            new java.util.concurrent.atomic.AtomicReference<>(null);

    /** 按目标 host 注入 Authorization:仅用户自配通道(key 永不出原生层)。 */
    private void injectAuth(HttpURLConnection conn, String url) {
        String host = null;
        try { host = new URL(url).getHost(); } catch (Exception ignored) {}
        if (host != null && host.equals(CUSTOM_HOST.get())
                && customAiKey != null && !customAiKey.isEmpty()) {
            conn.setRequestProperty("Authorization", "Bearer " + customAiKey);
        }
    }

    private void toast(final String msg) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                android.widget.Toast.makeText(MainActivity.this, msg, android.widget.Toast.LENGTH_SHORT).show();
            }
        });
    }

    /** 系统通知:后台研报完成/失败时弹出(通知栏)。 */
    private void notifyDone(String title, String text) {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            Notification.Builder b;
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "研报生成",
                        NotificationManager.IMPORTANCE_DEFAULT);
                ch.setDescription("后台生成研究报告完成提醒");
                nm.createNotificationChannel(ch);
                b = new Notification.Builder(this, CHANNEL_ID);
            } else {
                b = new Notification.Builder(this);
            }
            Intent i = new Intent(this, MainActivity.class);
            i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            PendingIntent pi = PendingIntent.getActivity(this, 1, i,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            b.setSmallIcon(android.R.drawable.stat_notify_chat)
                    .setContentTitle(title).setContentText(text)
                    .setStyle(new Notification.BigTextStyle().bigText(text))
                    .setAutoCancel(true).setContentIntent(pi);
            nm.notify(NOTIFY_ID, b.build());
        } catch (Exception ignored) { }
    }

    private static String jq(String s) {
        return "\"" + (s == null ? "" : s
                .replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", "\\n").replace("\r", "")) + "\"";
    }

    private class Bridge {

        /** 异步 HTTP:回调 window.__bridgeDone(cbId, status, body)。AI 域名自动注入对应 key。 */
        @JavascriptInterface
        public void requestAsync(final String cbId, final String method, final String url,
                                 final String headersJson, final String body) {
            if (!hostAllowed(url)) {
                post(cbId, 0, "{\"error\":\"host not allowed\"}");
                return;
            }
            pool.execute(new Runnable() {
                @Override
                public void run() {
                    try {
                        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                        conn.setRequestMethod(method);
                        conn.setConnectTimeout(15000);
                        conn.setReadTimeout(60000);
                        conn.setRequestProperty("User-Agent", "InvestmentSteward-Mobile/0.3");
                        conn.setRequestProperty("Connection", "close");
                        injectAuth(conn, url);
                        // headers 为扁平 {"k":"v"} JSON,手写解析避免额外依赖
                        String hs = headersJson == null ? "{}" : headersJson.trim();
                        if (hs.length() > 2) {
                            String inner = hs.substring(1, hs.length() - 1);
                            for (String pair : inner.split(",")) {
                                int colon = pair.indexOf(":");
                                if (colon <= 0) continue;
                                String k = unquote(pair.substring(0, colon).trim());
                                String v = unquote(pair.substring(colon + 1).trim());
                                if (!k.isEmpty() && !"authorization".equalsIgnoreCase(k)) {
                                    conn.setRequestProperty(k, v);
                                }
                            }
                        }
                        if (body != null && !body.isEmpty()) {
                            conn.setDoOutput(true);
                            byte[] out = body.getBytes(StandardCharsets.UTF_8);
                            conn.setFixedLengthStreamingMode(out.length);
                            OutputStream os = conn.getOutputStream();
                            os.write(out);
                            os.close();
                        }
                        int status = conn.getResponseCode();
                        InputStream is = status >= 400 ? conn.getErrorStream() : conn.getInputStream();
                        String resp = readAll(is, conn.getContentType());
                        post(cbId, status, resp);
                    } catch (Exception e) {
                        post(cbId, 0, "{\"error\":\"" + e.getMessage() + "\"}");
                    }
                }
            });
        }

        /** 设置页:登记自定义 AI 通道(host 进白名单,key 由原生注入,JS 侧不回读明文)。 */
        @JavascriptInterface
        public void setAiCustom(String url, String key) {
            try {
                String host = new URL(url == null ? "" : url).getHost();
                CUSTOM_HOST.set(host);
                customAiHost = host;
                customAiKey = (key == null || key.trim().isEmpty()) ? null : key.trim();
                toast(host == null ? "自定义通道已清除" : "自定义通道已接入:" + host);
            } catch (Exception e) {
                toast("自定义通道地址无效");
            }
        }

        /** 提交后台研报任务:原生线程跑 chat/completions,完成 → 系统通知 + __aiTaskDone 回调。 */
        @JavascriptInterface
        public void startAiTask(final String taskId, final String url, final String model, final String prompt) {
            final AiTask t = new AiTask();
            TASKS.put(taskId, t);
            if (!hostAllowed(url)) {
                t.status = "error";
                t.err = "host not allowed";
                return;
            }
            final String body = "{\"model\":" + jq(model)
                    + ",\"messages\":[{\"role\":\"user\",\"content\":" + jq(prompt) + "}],\"temperature\":0.5}";
            aiPool.execute(new Runnable() {
                @Override
                public void run() {
                    int attempts = 0;
                    String lastErr = null;
                    while (attempts < 3) {
                        try {
                            HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                            conn.setRequestMethod("POST");
                            conn.setConnectTimeout(20000);
                            conn.setReadTimeout(300000); // 生成可能要 1-3 分钟
                            conn.setRequestProperty("Content-Type", "application/json");
                            conn.setRequestProperty("User-Agent", "InvestmentSteward-Mobile/0.3");
                            conn.setRequestProperty("Connection", "close"); // 禁 keep-alive:复用陈旧 socket 会 "Software caused connection abort"
                            injectAuth(conn, url);
                            byte[] out = body.getBytes(StandardCharsets.UTF_8);
                            conn.setFixedLengthStreamingMode(out.length);
                            conn.setDoOutput(true);
                            OutputStream os = conn.getOutputStream();
                            os.write(out);
                            os.close();
                            int code = conn.getResponseCode();
                            String resp = readAll(code >= 400 ? conn.getErrorStream() : conn.getInputStream(), conn.getContentType());
                            if (code == 200) {
                                t.raw = resp;
                                t.status = "done";
                                notifyDone("研报生成完成", "后台任务已结束,打开应用即可查看全文。");
                                break;
                            } else if (code == 429) {
                                t.status = "error";
                                t.err = "限速中(内置通道 1 次/分钟),请稍后再试";
                                notifyDone("研报生成失败", "任务 " + taskId + " 失败:" + t.err);
                                break;
                            } else if (code >= 400 && code < 500) {
                                t.status = "error";
                                t.err = "HTTP " + code + ":" + resp.substring(0, Math.min(resp.length(), 180));
                                notifyDone("研报生成失败", "任务 " + taskId + " 失败:" + t.err);
                                break;
                            } else {
                                lastErr = "HTTP " + code + ":" + resp.substring(0, Math.min(resp.length(), 120)); // 5xx 可重试
                            }
                        } catch (Exception e) {
                            lastErr = e.getMessage() == null ? "network error" : e.getMessage();
                        }
                        attempts++;
                        if (attempts < 3) {
                            try { Thread.sleep(3000); } catch (InterruptedException ie) { break; }
                        }
                    }
                    if (!"done".equals(t.status) && !"error".equals(t.status)) {
                        t.status = "error";
                        t.err = (lastErr == null ? "unknown" : lastErr) + "(已重试 " + attempts + " 次)";
                        notifyDone("研报生成失败", "任务 " + taskId + " 失败:" + t.err);
                    }
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            if (web != null) web.evaluateJavascript(
                                    "window.__aiTaskDone && window.__aiTaskDone(" + jq(taskId) + ");", null);
                        }
                    });
                }
            });
        }

        /** 同步查询后台任务状态(页面回到工作台时轮询):{"status":..,"err":..,"raw":..}。 */
        @JavascriptInterface
        public String getAiTask(String taskId) {
            AiTask t = TASKS.get(taskId);
            if (t == null) return "{\"status\":\"lost\"}";
            return "{\"status\":\"" + t.status + "\",\"err\":" + jq(t.err) + ",\"raw\":" + jq(t.raw) + "}";
        }

        /** 保存研报文本:API29+ 走 MediaStore Downloads(无需权限),旧版本落应用文档目录。 */
        @JavascriptInterface
        public String saveTextFile(String name, String text) {
            try {
                if (name == null || name.trim().isEmpty()) name = "research-report.txt";
                byte[] data = (text == null ? "" : text).getBytes(StandardCharsets.UTF_8);
                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues cv = new ContentValues();
                    cv.put(MediaStore.Downloads.DISPLAY_NAME, name);
                    cv.put(MediaStore.Downloads.MIME_TYPE, "text/plain");
                    cv.put(MediaStore.Downloads.IS_PENDING, 1);
                    Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
                    if (uri == null) { toast("保存失败:系统拒绝写入下载"); return "err:insert"; }
                    OutputStream os = getContentResolver().openOutputStream(uri);
                    os.write(data);
                    os.close();
                    cv.clear();
                    cv.put(MediaStore.Downloads.IS_PENDING, 0);
                    getContentResolver().update(uri, cv, null, null);
                    toast("已保存到系统下载:" + name);
                    return "ok:" + name;
                }
                File dir = getExternalFilesDir(Environment.DIRECTORY_DOCUMENTS);
                if (dir == null) dir = getFilesDir();
                File f = new File(dir, name);
                FileOutputStream fo = new FileOutputStream(f);
                fo.write(data);
                fo.close();
                toast("已保存:" + f.getAbsolutePath());
                return "ok:" + f.getAbsolutePath();
            } catch (Exception e) {
                toast("保存失败:" + e.getMessage());
                return "err:" + e.getMessage();
            }
        }

        private void post(final String cbId, final int status, final String body) {
            final String payload = body == null ? "" : body
                    .replace("\\", "\\\\").replace("'", "\\'")
                    .replace("\n", "\\n").replace("\r", "\\r");
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    web.evaluateJavascript(
                            "window.__bridgeDone('" + cbId + "'," + status + ",'" + payload + "');", null);
                }
            });
        }

        private String unquote(String s) {
            if (s.length() >= 2 && s.startsWith("\"") && s.endsWith("\"")) {
                return s.substring(1, s.length() - 1).replace("\\\"", "\"").replace("\\\\", "\\");
            }
            return s;
        }

    /** 读响应体:按 Content-Type charset 解码(新浪榜单是 GBK),默认 UTF-8;UTF-8 出现乱码符时回退 GBK 再试。 */
    private String readAll(InputStream is, String contentType) throws Exception {
        if (is == null) return "";
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) > 0 && bos.size() < 4 * 1024 * 1024) bos.write(buf, 0, n);
        byte[] raw = bos.toByteArray();
        String lower = contentType == null ? "" : contentType.toLowerCase();
        if (lower.contains("gbk") || lower.contains("gb2312")) {
            return new String(raw, "GBK");
        }
        String utf8 = new String(raw, StandardCharsets.UTF_8);
        if (utf8.indexOf('\uFFFD') >= 0) {
            try { return new String(raw, "GBK"); } catch (Exception ignored) { }
        }
        return utf8;
    }
    }
}
