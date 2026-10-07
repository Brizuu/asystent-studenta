package pl.zenfix.asystent;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;
import android.webkit.CookieManager;
import android.webkit.GeolocationPermissions;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Scanner;

/**
 * Asystent na Androida: natywne okno z wersją webową (zenfix.pl/asystent/app).
 * Konto, synchronizacja, notatki, plan, Dojazd i AI działają jak w przeglądarce, a aplikacja dokłada to,
 * czego zwykła strona nie ma: wybór plików, zapis pobranych plików, lokalizację, przycisk Wstecz i aktualizacje.
 */
public class MainActivity extends Activity {
    static final String START = "https://zenfix.pl/asystent/app/";
    static final String HOST = "zenfix.pl";
    static final String RELEASES = "https://api.github.com/repos/Brizuu/asystent-studenta/releases/latest";
    static final int REQ_FILE = 1, REQ_GEO = 2;

    WebView web;
    ValueCallback<Uri[]> fileCb;
    GeolocationPermissions.Callback geoCb;
    String geoOrigin;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        web.setBackgroundColor(0xFF0B0B14);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);           // localStorage: token konta, ustawienia
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setTextZoom(100);
        s.setGeolocationEnabled(true);
        s.setUserAgentString(s.getUserAgentString() + " AsystentAndroid/" + version());
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        web.addJavascriptInterface(new Bridge(), "AsystentAndroid");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        web.setDownloadListener((url, ua, disposition, mime, length) -> download(url, ua, disposition, mime));

        if (state != null) web.restoreState(state);
        else web.loadUrl(startUrl(getIntent()));
        checkUpdate();
    }

    String startUrl(Intent i) {
        Uri u = i != null ? i.getData() : null;
        return u != null && HOST.equals(u.getHost()) ? u.toString() : START;
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        if (onInstallResult(i)) return;
        if (i.getData() != null) web.loadUrl(startUrl(i));
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();   // logowanie przetrwa zamknięcie aplikacji
        web.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
        // wrócił z ustawień „Instaluj nieznane aplikacje” — dokończ aktualizację
        if (pendingInstall && (Build.VERSION.SDK_INT < 26 || getPackageManager().canRequestPackageInstalls())) {
            pendingInstall = false;
            installUpdate();
        }
    }

    @Override
    public void onBackPressed() {
        // aplikacja ma własną historię (widoki, notatki) — Wstecz cofa w niej, a dopiero na początku zamyka
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    String version() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "0";
        }
    }

    /* ---------- strony: zenfix.pl w aplikacji, reszta (Spotify, PKP, mapy) w przeglądarce ---------- */
    class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            String scheme = u.getScheme() == null ? "" : u.getScheme();
            if ((scheme.equals("https") || scheme.equals("http")) && HOST.equals(u.getHost())) return false;
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, u));
            } catch (ActivityNotFoundException e) {
                Toast.makeText(MainActivity.this, "Nie ma aplikacji do otwarcia tego linku", Toast.LENGTH_SHORT).show();
            }
            return true;
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame()) offline();
        }
    }

    void offline() {
        String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;"
                + "background:#0b0b14;color:#eae8f2;font-family:sans-serif;text-align:center;padding:24px;box-sizing:border-box}"
                + "h2{margin:0 0 8px}p{color:#a5a3b5;margin:0 0 24px;line-height:1.5}"
                + "button{border:0;border-radius:14px;padding:14px 26px;font-size:16px;font-weight:700;background:#a9b2ff;color:#14142a}</style></head>"
                + "<body><h2>Brak połączenia</h2><p>Asystent potrzebuje internetu, żeby pobrać Twoje dane.<br>Sprawdź połączenie i spróbuj ponownie.</p>"
                + "<button onclick=\"location.href='" + START + "'\">Spróbuj ponownie</button></body></html>";
        web.loadDataWithBaseURL(START, html, "text/html", "utf-8", START);
    }

    /* ---------- wybór plików (PDF, zdjęcia, zrzuty do sugestii) i lokalizacja ---------- */
    class Chrome extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> cb, FileChooserParams params) {
            if (fileCb != null) fileCb.onReceiveValue(null);
            fileCb = cb;
            try {
                Intent i = params.createIntent();
                if (params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                startActivityForResult(i, REQ_FILE);
            } catch (ActivityNotFoundException e) {
                fileCb = null;
                return false;
            }
            return true;
        }

        @Override
        public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback cb) {
            if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
                cb.invoke(origin, true, false);
            } else {
                geoCb = cb;
                geoOrigin = origin;
                requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_GEO);
            }
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != REQ_FILE || fileCb == null) return;
        Uri[] out = null;
        if (res == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                out = new Uri[data.getClipData().getItemCount()];
                for (int k = 0; k < out.length; k++) out[k] = data.getClipData().getItemAt(k).getUri();
            } else if (data.getData() != null) {
                out = new Uri[]{data.getData()};
            }
        }
        fileCb.onReceiveValue(out);
        fileCb = null;
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        if (req == REQ_GEO && geoCb != null) {
            boolean ok = res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED;
            geoCb.invoke(geoOrigin, ok, false);
            geoCb = null;
        }
    }

    /* ---------- pobieranie: zwykłe linki przez DownloadManager, pliki z pamięci strony (blob:, np. eksport PDF) przez most JS ---------- */
    void download(String url, String ua, String disposition, String mime) {
        String name = URLUtil.guessFileName(url, disposition, mime);
        if (url.startsWith("blob:") || url.startsWith("data:")) {
            String js = "(async()=>{try{const b=await (await fetch('" + url + "')).blob();const r=new FileReader();"
                    + "r.onload=()=>AsystentAndroid.saveFile(r.result.split(',')[1],b.type||'" + mime + "','" + name.replace("'", "") + "');"
                    + "r.readAsDataURL(b);}catch(e){AsystentAndroid.toast('Nie udało się zapisać pliku');}})()";
            web.evaluateJavascript(js, null);
            return;
        }
        try {
            DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
            r.addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url));
            r.addRequestHeader("User-Agent", ua);
            r.setMimeType(mime);
            r.setTitle(name);
            r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(r);
            Toast.makeText(this, "Pobieram: " + name, Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        }
    }

    class Bridge {
        @JavascriptInterface
        public void saveFile(String b64, String mime, String name) {
            try {
                byte[] data = Base64.decode(b64, Base64.DEFAULT);
                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues v = new ContentValues();
                    v.put(MediaStore.Downloads.DISPLAY_NAME, name);
                    v.put(MediaStore.Downloads.MIME_TYPE, mime);
                    Uri u = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                    try (OutputStream o = getContentResolver().openOutputStream(u)) { o.write(data); }
                } else {
                    File dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
                    try (FileOutputStream o = new FileOutputStream(new File(dir, name))) { o.write(data); }
                }
                toast("Zapisano w Pobranych: " + name);
            } catch (Exception e) {
                toast("Nie udało się zapisać pliku");
            }
        }

        @JavascriptInterface
        public void toast(String msg) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, msg, Toast.LENGTH_LONG).show());
        }

        @JavascriptInterface
        public String version() {
            return MainActivity.this.version();
        }

        /** Ustawienia → Dane → „Sprawdź teraz”: wynik trafia do window.androidUpdate(json). */
        @JavascriptInterface
        public void checkUpdate() {
            new Thread(() -> toPage("androidUpdate", fetchRelease())).start();
        }

        /** „Zaktualizuj teraz”: pobranie z postępem (window.androidUpdateProgress) i instalacja. */
        @JavascriptInterface
        public void installUpdate() {
            runOnUiThread(MainActivity.this::installUpdate);
        }
    }

    /* ---------- aktualizacje: najnowsze wydanie na GitHubie z plikiem Asystent.apk ----------
       Sprawdzenie przy starcie (okno „Nowa wersja”) i z Ustawień → Dane (most JS). Instalacja w aplikacji:
       pobranie APK z postępem i PackageInstaller — bez szukania pliku na stronie. */
    volatile String updLatest, updUrl, updNotes;
    volatile boolean updBusy;

    /** Pobiera informacje o najnowszym wydaniu; zwraca JSON dla strony. */
    JSONObject fetchRelease() {
        JSONObject out = new JSONObject();
        try {
            out.put("current", version());
            HttpURLConnection c = (HttpURLConnection) new URL(RELEASES).openConnection();
            c.setRequestProperty("Accept", "application/vnd.github+json");
            c.setConnectTimeout(8000);
            c.setReadTimeout(8000);
            String body;
            try (InputStream in = c.getInputStream(); Scanner sc = new Scanner(in, "UTF-8").useDelimiter("\\A")) {
                body = sc.hasNext() ? sc.next() : "";
            }
            JSONObject rel = new JSONObject(body);
            String latest = rel.optString("tag_name", "").replaceFirst("^v", "");
            String apk = null;
            JSONArray assets = rel.optJSONArray("assets");
            for (int k = 0; assets != null && k < assets.length(); k++) {
                JSONObject a = assets.getJSONObject(k);
                if ("Asystent.apk".equals(a.optString("name"))) apk = a.optString("browser_download_url");
            }
            updLatest = latest;
            updUrl = apk;
            updNotes = rel.optString("body", "");
            out.put("latest", latest);
            out.put("published", rel.optString("published_at", ""));
            out.put("notes", updNotes.length() > 1500 ? updNotes.substring(0, 1500) : updNotes);
            out.put("available", apk != null && newer(latest, version()));
        } catch (Exception e) {
            try { out.put("error", "Nie udało się sprawdzić aktualizacji — sprawdź internet i spróbuj ponownie."); } catch (Exception ignored) { }
        }
        return out;
    }

    void checkUpdate() {
        new Thread(() -> {
            JSONObject r = fetchRelease();
            if (r.optBoolean("available")) {
                runOnUiThread(() -> new AlertDialog.Builder(this)
                        .setTitle("Nowa wersja " + updLatest)
                        .setMessage("Jest nowsza wersja aplikacji Asystent. Zainstalować ją teraz? Twoje dane zostają.")
                        .setPositiveButton("Zaktualizuj", (d, w) -> installUpdate())
                        .setNegativeButton("Później", null)
                        .show());
            }
        }).start();
    }

    /** Wynik/postęp dla strony: window.androidUpdate(json) i window.androidUpdateProgress(json). */
    void toPage(String fn, JSONObject o) {
        String js = "window." + fn + "&&window." + fn + "(" + o.toString() + ")";
        runOnUiThread(() -> web.evaluateJavascript(js, null));
    }

    void progress(String state, long done, long total, String err) {
        try {
            JSONObject o = new JSONObject();
            o.put("state", state);
            o.put("done", done);
            o.put("total", total);
            if (err != null) o.put("error", err);
            toPage("androidUpdateProgress", o);
        } catch (Exception ignored) { }
    }

    void installUpdate() {
        if (updBusy) return;
        // Android 8+: pierwsza aktualizacja wymaga zgody „Instaluj nieznane aplikacje” dla Asystenta
        if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
            pendingInstall = true;
            Toast.makeText(this, "Zezwól Asystentowi na instalowanie aktualizacji i wróć do aplikacji", Toast.LENGTH_LONG).show();
            try {
                startActivity(new Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + getPackageName())));
            } catch (Exception e) {
                progress("error", 0, 0, "Włącz w ustawieniach telefonu: Aplikacje → Asystent → Instalowanie nieznanych aplikacji.");
            }
            return;
        }
        updBusy = true;
        progress("downloading", 0, 0, null);
        new Thread(() -> {
            try {
                if (updUrl == null) fetchRelease();
                if (updUrl == null) throw new Exception("Brak pliku Asystent.apk w najnowszym wydaniu.");
                File apk = new File(getCacheDir(), "Asystent-update.apk");
                HttpURLConnection c = (HttpURLConnection) new URL(updUrl).openConnection();
                c.setInstanceFollowRedirects(true);
                c.setConnectTimeout(15000);
                c.setReadTimeout(30000);
                long total = c.getContentLengthLong(), done = 0, last = 0;
                try (InputStream in = c.getInputStream(); FileOutputStream o = new FileOutputStream(apk)) {
                    byte[] buf = new byte[65536];
                    for (int n; (n = in.read(buf)) > 0; ) {
                        o.write(buf, 0, n);
                        done += n;
                        long now = System.currentTimeMillis();
                        if (now - last > 250) { last = now; progress("downloading", done, total, null); }
                    }
                }
                progress("installing", done, total, null);
                android.content.pm.PackageInstaller pi = getPackageManager().getPackageInstaller();
                android.content.pm.PackageInstaller.SessionParams sp =
                        new android.content.pm.PackageInstaller.SessionParams(android.content.pm.PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                sp.setSize(apk.length());
                int id = pi.createSession(sp);
                try (android.content.pm.PackageInstaller.Session ses = pi.openSession(id)) {
                    try (InputStream in = new java.io.FileInputStream(apk); OutputStream o = ses.openWrite("asystent.apk", 0, apk.length())) {
                        byte[] buf = new byte[65536];
                        for (int n; (n = in.read(buf)) > 0; ) o.write(buf, 0, n);
                        ses.fsync(o);
                    }
                    Intent cb = new Intent(this, MainActivity.class).setAction(ACTION_INSTALL);
                    int fl = android.app.PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? 0x02000000 /* FLAG_MUTABLE */ : 0);
                    ses.commit(android.app.PendingIntent.getActivity(this, 7, cb, fl).getIntentSender());
                }
            } catch (Exception e) {
                progress("error", 0, 0, e.getMessage() != null ? e.getMessage() : "Nie udało się pobrać aktualizacji.");
            } finally {
                updBusy = false;
            }
        }).start();
    }

    static final String ACTION_INSTALL = "pl.zenfix.asystent.INSTALL";
    boolean pendingInstall;

    /** Odpowiedź instalatora systemowego: prośba o potwierdzenie albo błąd. */
    boolean onInstallResult(Intent i) {
        if (i == null || !ACTION_INSTALL.equals(i.getAction())) return false;
        int st = i.getIntExtra(android.content.pm.PackageInstaller.EXTRA_STATUS, -999);
        if (st == android.content.pm.PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent confirm = (Intent) i.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm != null) startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } else if (st != android.content.pm.PackageInstaller.STATUS_SUCCESS) {
            String msg = i.getStringExtra(android.content.pm.PackageInstaller.EXTRA_STATUS_MESSAGE);
            progress("error", 0, 0, st == android.content.pm.PackageInstaller.STATUS_FAILURE_ABORTED
                    ? "Instalacja anulowana." : "Instalacja nie powiodła się" + (msg != null ? ": " + msg : "."));
        }
        return true;
    }

    static boolean newer(String a, String b) {
        String[] x = a.split("\\."), y = b.split("\\.");
        for (int k = 0; k < Math.max(x.length, y.length); k++) {
            int p = k < x.length ? parse(x[k]) : 0, q = k < y.length ? parse(y[k]) : 0;
            if (p != q) return p > q;
        }
        return false;
    }

    static int parse(String s) {
        try {
            return Integer.parseInt(s.replaceAll("\\D", ""));
        } catch (Exception e) {
            return 0;
        }
    }
}
