package com.bottrack.app;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import com.google.mlkit.vision.barcode.common.Barcode;
import com.google.mlkit.vision.codescanner.GmsBarcodeScanner;
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions;
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * BotTrack for Android: the BotTrack pages served by the tester's own Mac, in an app.
 * The first time it opens it scans the QR code on the Mac's Setup page, Phone access. The code
 * holds the Mac's phone link, a key that pairs this phone, and a private topic on ntfy.sh where
 * the Mac posts its new link whenever the old one stops working, so the app follows it by itself.
 * Links to anywhere else (carrier tracking pages) open in the browser.
 */
public class MainActivity extends Activity {
    private static final int NAVY = Color.parseColor("#132048");
    private static final int SURFACE = Color.parseColor("#111c46");
    private static final int TEXT = Color.parseColor("#e8eeff");
    private static final int MUTED = Color.parseColor("#8e9bc6");
    private static final int ACCENT = Color.parseColor("#3dd8ef");
    private static final String LINK = "^https://[a-z0-9-]+\\.trycloudflare\\.com$";

    private SharedPreferences prefs;
    private WebView web;
    private Uri home;
    private boolean lookedForNewLink;
    private final Handler main = new Handler(Looper.getMainLooper());
    private static final int CONNECT_TIMEOUT_MS = 30000;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = getSharedPreferences("bottrack", MODE_PRIVATE);
        String address = prefs.getString("address", "");
        if (address.isEmpty()) {
            showSetup(null);
        } else if (prefs.getString("key", "").isEmpty() && address.startsWith("http://100.")) {
            // Saved by version 2 for Tailscale, which BotTrack no longer uses.
            showSetup("BotTrack now connects with a QR code instead of Tailscale. Scan the one on your Mac's Setup page, Phone access.");
        } else {
            showWeb(firstPage());
        }
    }

    /** The Mac's address ending in "/", e.g. "https://quiet-words.trycloudflare.com/". */
    private String address() {
        return prefs.getString("address", "");
    }

    /** Where to open BotTrack: through the pairing link when we have the key, so a new link signs in too. */
    private String firstPage() {
        String key = prefs.getString("key", "");
        return key.isEmpty() ? address() : address() + "m/pair?k=" + Uri.encode(key);
    }

    /**
     * Read the QR code (or a pasted link). "https://x.trycloudflare.com/m/pair?k=KEY&t=TOPIC" pairs;
     * a plain address like "192.168.1.20:8765" works for a phone on the same Wi-Fi.
     */
    private boolean use(String scanned) {
        String s = scanned == null ? "" : scanned.trim();
        if (s.isEmpty()) return false;
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://" + s;
        Uri u = Uri.parse(s);
        if (u.getHost() == null || u.getHost().isEmpty()) return false;
        String port = u.getPort() == -1 ? ("http".equals(u.getScheme()) ? ":8765" : "") : ":" + u.getPort();
        SharedPreferences.Editor e = prefs.edit().putString("address", u.getScheme() + "://" + u.getHost() + port + "/");
        if ("/m/pair".equals(u.getPath()) && u.getQueryParameter("k") != null) {
            e.putString("key", u.getQueryParameter("k"));
            e.putString("topic", u.getQueryParameter("t") == null ? "" : u.getQueryParameter("t"));
        }
        e.apply();
        return true;
    }

    private void scan() {
        GmsBarcodeScannerOptions options = new GmsBarcodeScannerOptions.Builder()
                .setBarcodeFormats(Barcode.FORMAT_QR_CODE).build();
        GmsBarcodeScanner scanner = GmsBarcodeScanning.getClient(this, options);
        scanner.startScan()
                .addOnSuccessListener(code -> {
                    if (use(code.getRawValue())) {
                        lookedForNewLink = false;
                        showWeb(firstPage());
                    } else {
                        showSetup("That isn't BotTrack's QR code. Scan the one on your Mac's Setup page, Phone access.");
                    }
                })
                .addOnFailureListener(err -> showSetup("Couldn't open the scanner (" + err.getMessage()
                        + "). Try again in a minute, or paste the link from your Mac below."));
    }

    private void showSetup(String problem) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackgroundColor(NAVY);
        int pad = dp(24);
        box.setPadding(pad, dp(72), pad, pad);

        TextView title = text("BotTrack", 30, TEXT);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        box.addView(title);
        TextView help = text("On your Mac, open BotTrack, go to Setup, Phone access, turn it on, and scan the QR code.", 15, MUTED);
        help.setPadding(0, dp(10), 0, dp(20));
        box.addView(help);
        if (problem != null) {
            TextView err = text(problem, 15, Color.parseColor("#ff6b7a"));
            err.setPadding(0, 0, 0, dp(16));
            box.addView(err);
        }

        Button scan = button("Scan QR code", true);
        box.addView(scan, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(56)));
        scan.setOnClickListener(v -> scan());

        TextView or = text("Or paste the link shown under the code:", 14, MUTED);
        or.setPadding(0, dp(32), 0, dp(8));
        box.addView(or);
        EditText input = new EditText(this);
        input.setHint("https://….trycloudflare.com");
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setTextColor(TEXT);
        input.setHintTextColor(MUTED);
        input.setBackgroundColor(SURFACE);
        input.setPadding(dp(14), dp(14), dp(14), dp(14));
        box.addView(input, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        Button go = button("Connect", false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        lp.topMargin = dp(12);
        box.addView(go, lp);
        go.setOnClickListener(v -> {
            if (!use(input.getText().toString())) {
                input.setError("Paste the link from BotTrack's Setup page");
                return;
            }
            lookedForNewLink = false;
            showWeb(firstPage());
        });
        web = null;
        setContentView(box);
    }

    private void showWeb(String page) {
        home = Uri.parse(address());
        final FrameLayout frame = new FrameLayout(this);
        frame.setBackgroundColor(NAVY);
        final TextView connecting = text("Connecting to your Mac…", 16, MUTED);
        final boolean[] loaded = {false};
        web = new WebView(this);
        web.setBackgroundColor(NAVY);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);  // confirm() before deletes
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSaveFormData(false);  // profile fields hold personal details
        // Tells the pages this is the app and which version, for "Scan a new QR code" and app updates.
        s.setUserAgentString(s.getUserAgentString() + " BotTrackApp/" + BuildConfig.VERSION_CODE);
        CookieManager.getInstance().setAcceptCookie(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if ("bottrack".equals(u.getScheme())) {
                    scan();
                    return true;
                }
                if (sameServer(u, home)) return false;
                startActivity(new Intent(Intent.ACTION_VIEW, u));  // tracking pages and other sites
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError error) {
                if (view == web && req.isForMainFrame()) unreachable();
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest req, WebResourceResponse resp) {
                // Cloudflare answers 530 (or 502) once the Mac's old link has closed.
                int code = resp.getStatusCode();
                if (view == web && req.isForMainFrame() && (code == 530 || code == 502) && sameServer(req.getUrl(), home)) unreachable();
            }

            @Override
            public void onPageCommitVisible(WebView view, String url) {
                if (view != web) return;  // the Mac answered: stop the connect timer and the "Connecting" note
                loaded[0] = true;
                frame.removeView(connecting);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();  // stay signed in after the app is closed
                if (view != web) return;
                lookedForNewLink = false;  // reached the Mac, so look again next time it's gone
                loaded[0] = true;
                frame.removeView(connecting);
            }
        });
        frame.addView(web);
        frame.addView(connecting, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER));
        setContentView(frame);
        web.loadUrl(page);
        // An address that never answers (no error, just waiting) would leave a blank screen.
        final WebView started = web;
        main.postDelayed(() -> {
            if (web == started && !loaded[0]) unreachable();
        }, CONNECT_TIMEOUT_MS);
    }

    /** The Mac didn't answer: ask its private topic for a newer link, once, before giving up. */
    private void unreachable() {
        web.stopLoading();
        web = null;  // ignore anything more from this page
        String topic = prefs.getString("topic", "");
        if (topic.isEmpty() || lookedForNewLink) {
            showOffline();
            return;
        }
        lookedForNewLink = true;
        new Thread(() -> {
            String link = latestLink(topic);
            runOnUiThread(() -> {
                if (link != null && !(link + "/").equals(address())) {
                    prefs.edit().putString("address", link + "/").apply();
                    showWeb(firstPage());
                } else {
                    showOffline();
                }
            });
        }).start();
    }

    /** The newest link the Mac posted to ntfy.sh (kept there for 12 hours; the Mac posts again every 6). */
    static String latestLink(String topic) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("https://ntfy.sh/" + Uri.encode(topic) + "/raw?poll=1&since=all").openConnection();
            c.setConnectTimeout(10000);
            c.setReadTimeout(10000);
            String last = null;
            try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = r.readLine()) != null; ) {
                    if (line.trim().matches(LINK)) last = line.trim();
                }
            }
            return last;
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private void showOffline() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackgroundColor(NAVY);
        int pad = dp(24);
        box.setPadding(pad, dp(72), pad, pad);
        TextView title = text("Can't reach your Mac", 26, TEXT);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        box.addView(title);
        TextView help = text("Check that your Mac is awake, online, and BotTrack is open with phone access on. "
                + "If you pressed Forget all phones, scan the new QR code.", 15, MUTED);
        help.setPadding(0, dp(10), 0, dp(24));
        box.addView(help);
        Button retry = button("Try again", true);
        box.addView(retry, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(56)));
        retry.setOnClickListener(v -> {
            lookedForNewLink = false;
            showWeb(firstPage());
        });
        Button scan = button("Scan QR code", false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        lp.topMargin = dp(12);
        box.addView(scan, lp);
        scan.setOnClickListener(v -> scan());
        web = null;
        setContentView(box);
    }

    static boolean sameServer(Uri a, Uri b) {
        return a.getHost() != null && b != null && a.getHost().equalsIgnoreCase(b.getHost()) && a.getPort() == b.getPort()
                && String.valueOf(a.getScheme()).equals(b.getScheme());
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else if (web == null && !address().isEmpty()) {
            showWeb(firstPage());
        } else {
            super.onBackPressed();
        }
    }

    private Button button(String label, boolean primary) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(16);
        b.setTextColor(primary ? Color.parseColor("#04122a") : TEXT);
        b.setBackgroundColor(primary ? ACCENT : SURFACE);
        return b;
    }

    private TextView text(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setGravity(Gravity.START);
        return t;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }
}
