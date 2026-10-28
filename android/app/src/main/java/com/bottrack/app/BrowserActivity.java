package com.bottrack.app;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.webkit.HttpAuthHandler;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.webkit.ProfileStore;
import androidx.webkit.ProxyConfig;
import androidx.webkit.ProxyController;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import org.json.JSONObject;

import java.util.Collections;

/**
 * One of BotTrack's browsers on the phone: a page that goes through the browser's proxy, with its own cookies.
 *
 * The proxy is set for the whole app while this screen is open (Android lets an app pick one proxy at a time),
 * except for the Mac's own link so BotTrack keeps working; it's cleared when the browser closes. The app signs in
 * to the proxy itself when it asks. Each browser has its own WebView profile: "Save session" keeps it, so the
 * next open is still signed in; otherwise a fresh one is made and deleted on close. WebRTC is switched off in
 * the pages so it can't reach the internet around the proxy.
 */
public class BrowserActivity extends Activity {
    private static final int NAVY = Color.parseColor("#0a1330");
    private static final int BAR = Color.parseColor("#0d1736");
    private static final int SURFACE = Color.parseColor("#121d42");
    private static final int RAISED = Color.parseColor("#1c2a57");
    private static final int LINE = Color.parseColor("#26345e");
    private static final int TEXT = Color.parseColor("#e8eefc");
    private static final int MUTED = Color.parseColor("#a9b6d6");
    private static final int GREEN = Color.parseColor("#6ee7a8");
    private static final int RED = Color.parseColor("#ff8a8a");
    private static final String NO_WEBRTC = "(function(){['RTCPeerConnection','webkitRTCPeerConnection','RTCDataChannel']"
            + ".forEach(function(k){try{Object.defineProperty(window,k,{value:undefined,configurable:false});}catch(e){}});})();";

    private WebView web;
    private EditText address;
    private JSONObject proxy;
    private String profile;
    private boolean keep;
    private boolean proxySet;
    private int authTries;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        JSONObject cfg;
        try {
            cfg = new JSONObject(getIntent().getStringExtra("config"));
        } catch (Exception e) {
            finish();
            return;
        }
        proxy = cfg.optJSONObject("proxy");
        keep = cfg.optBoolean("keep", true);
        int id = cfg.optInt("id");
        profile = keep ? "browser-" + id : "fresh-" + id + "-" + System.currentTimeMillis();
        String mac = getIntent().getStringExtra("mac");
        dropOldFresh();

        if (proxy != null) {
            if (!WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE)) {
                Toast.makeText(this, "Update Android System WebView in the Play Store to use proxies here.", Toast.LENGTH_LONG).show();
                finish();
                return;
            }
            ProxyConfig.Builder b = new ProxyConfig.Builder()
                    .addProxyRule(proxy.optString("host") + ":" + proxy.optInt("port"));
            if (mac != null && !mac.isEmpty()) b.addBypassRule(mac);  // BotTrack itself never goes through it
            build(cfg);
            proxySet = true;
            // Nothing loads until the proxy is in place, so the first page can't go out on the phone's own IP.
            ProxyController.getInstance().setProxyOverride(b.build(), r -> runOnUiThread(r),
                    () -> { if (web != null) web.loadUrl(cfg.optString("url", "https://www.google.com")); });
            return;
        }
        build(cfg);
        web.loadUrl(cfg.optString("url", "https://www.google.com"));
    }

    /** Fresh sessions left behind (the app was closed before one could be deleted) go now. */
    private static void dropOldFresh() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE)) return;
        for (String name : ProfileStore.getInstance().getAllProfileNames()) {
            if (name.startsWith("fresh-")) {
                try {
                    ProfileStore.getInstance().deleteProfile(name);
                } catch (Exception ignored) {
                    // in use
                }
            }
        }
    }

    private void build(JSONObject cfg) {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(NAVY);

        // Top: back to the list, the address, reload; then which proxy and session this is.
        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.VERTICAL);
        top.setBackgroundColor(BAR);
        top.setPadding(dp(14), dp(14), dp(14), dp(12));
        LinearLayout row = new LinearLayout(this);
        row.setGravity(Gravity.CENTER_VERTICAL);
        TextView list = square("‹", "Back to Browsers");
        list.setOnClickListener(v -> finish());
        row.addView(list, new LinearLayout.LayoutParams(dp(44), dp(44)));
        address = new EditText(this);
        address.setSingleLine(true);
        address.setTextSize(15);
        address.setTextColor(TEXT);
        address.setHintTextColor(MUTED);
        address.setHint("Search or type an address");
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setImeOptions(EditorInfo.IME_ACTION_GO);
        address.setPadding(dp(12), 0, dp(12), 0);
        address.setBackground(round(SURFACE, LINE, 14));
        address.setOnEditorActionListener((v, action, ev) -> {
            if (action == EditorInfo.IME_ACTION_GO || (ev != null && ev.getKeyCode() == KeyEvent.KEYCODE_ENTER)) {
                go(address.getText().toString());
                return true;
            }
            return false;
        });
        LinearLayout.LayoutParams ap = new LinearLayout.LayoutParams(0, dp(44), 1);
        ap.leftMargin = dp(8);
        ap.rightMargin = dp(8);
        row.addView(address, ap);
        TextView reload = square("↻", "Reload");
        reload.setOnClickListener(v -> web.reload());
        row.addView(reload, new LinearLayout.LayoutParams(dp(44), dp(44)));
        top.addView(row);

        LinearLayout chips = new LinearLayout(this);
        chips.setGravity(Gravity.CENTER_VERTICAL);
        chips.setPadding(0, dp(10), 0, 0);
        String where = proxy == null ? "No proxy" : "● " + (proxy.optString("group").isEmpty() ? "Proxy" : proxy.optString("group"))
                + (proxy.optString("place").isEmpty() ? "" : " · " + proxy.optString("place"));
        chips.addView(chip(where, proxy == null ? MUTED : GREEN, proxy == null ? RAISED : Color.parseColor("#1f3d3a")));
        TextView sess = chip(keep ? "Session saved" : "Fresh session", Color.parseColor("#c9d4ef"), RAISED);
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(30));
        sp.leftMargin = dp(8);
        chips.addView(sess, sp);
        JSONObject account = cfg.optJSONObject("account");
        if (account != null) {
            TextView who = text(account.optString("site") + " · " + account.optString("email"), 13, MUTED);
            who.setGravity(Gravity.END);
            who.setSingleLine(true);
            who.setEllipsize(TextUtils.TruncateAt.MIDDLE);
            LinearLayout.LayoutParams wp = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1);
            wp.leftMargin = dp(8);
            chips.addView(who, wp);
        }
        top.addView(chips);
        root.addView(top);

        web = new WebView(this);
        if (WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE)) {
            ProfileStore.getInstance().getOrCreateProfile(profile);
            WebViewCompat.setProfile(web, profile);
        } else if (!keep) {
            Toast.makeText(this, "This phone's WebView can't keep sessions apart. Update Android System WebView.", Toast.LENGTH_LONG).show();
        }
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            WebViewCompat.addDocumentStartJavaScript(web, NO_WEBRTC, Collections.singleton("*"));
        }
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                if (!address.hasFocus()) address.setText(shortUrl(url));
            }

            @Override
            public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) {
                // The proxy asking for its login: answer it, but not over and over if it's wrong.
                if (proxy != null && host != null && host.equalsIgnoreCase(proxy.optString("host")) && authTries++ < 3) {
                    handler.proceed(proxy.optString("user"), proxy.optString("password"));
                } else {
                    if (proxy != null && authTries >= 3) {
                        Toast.makeText(BrowserActivity.this, "The proxy didn't accept its login. Check it on the Proxies page.", Toast.LENGTH_LONG).show();
                    }
                    handler.cancel();
                }
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));

        // Bottom: back, forward, copy the account's login, close.
        LinearLayout bottom = new LinearLayout(this);
        bottom.setBackgroundColor(BAR);
        bottom.setPadding(dp(12), dp(8), dp(12), dp(16));
        TextView back = tab("‹", "Back", TEXT);
        back.setOnClickListener(v -> { if (web.canGoBack()) web.goBack(); });
        TextView fwd = tab("›", "Forward", TEXT);
        fwd.setOnClickListener(v -> { if (web.canGoForward()) web.goForward(); });
        TextView copy = tab("⧉", "Copy login", TEXT);
        copy.setOnClickListener(v -> {
            if (account == null) {
                Toast.makeText(this, "No account is attached to this browser.", Toast.LENGTH_SHORT).show();
                return;
            }
            String login = account.optString("username").isEmpty() ? account.optString("email") : account.optString("username");
            ((ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("login", login));
            Toast.makeText(this, "Copied " + login, Toast.LENGTH_SHORT).show();
        });
        TextView close = tab("✕", "Close", RED);
        close.setOnClickListener(v -> finish());
        for (TextView t : new TextView[]{back, fwd, copy, close}) {
            bottom.addView(t, new LinearLayout.LayoutParams(0, dp(52), 1));
        }
        root.addView(bottom);
        setContentView(root);
    }

    private void go(String typed) {
        String t = typed.trim();
        if (t.isEmpty()) return;
        String url = t.matches("^[a-zA-Z]+://.*") ? t
                : (t.contains(".") && !t.contains(" ") ? "https://" + t : "https://www.google.com/search?q=" + Uri.encode(t));
        address.clearFocus();
        web.loadUrl(url);
    }

    static String shortUrl(String url) {
        return url == null ? "" : url.replaceFirst("^https?://(www\\.)?", "").replaceFirst("/$", "");
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            if (keep && WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE)) {
                ProfileStore.getInstance().getOrCreateProfile(profile).getCookieManager().flush();
            }
            web.destroy();
            web = null;
            if (!keep && WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE)) {
                try {
                    ProfileStore.getInstance().deleteProfile(profile);  // a fresh session leaves nothing behind
                } catch (Exception ignored) {
                    // still in use for a moment; it's removed with the next fresh one
                }
            }
        }
        if (proxySet) ProxyController.getInstance().clearProxyOverride(Runnable::run, () -> { });  // BotTrack and other browsers go direct again
        super.onDestroy();
    }

    private TextView square(String glyph, String label) {
        TextView t = text(glyph, 20, TEXT);
        t.setGravity(Gravity.CENTER);
        t.setContentDescription(label);
        t.setBackground(round(RAISED, RAISED, 14));
        return t;
    }

    private TextView chip(String s, int color, int fill) {
        TextView t = text(s, 13, color);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setGravity(Gravity.CENTER_VERTICAL);
        t.setPadding(dp(12), 0, dp(12), 0);
        t.setMinHeight(dp(30));
        t.setBackground(round(fill, fill, 15));
        return t;
    }

    private TextView tab(String glyph, String label, int color) {
        TextView t = text(glyph + "\n" + label, 12, color);
        t.setGravity(Gravity.CENTER);
        t.setContentDescription(label);
        return t;
    }

    private GradientDrawable round(int fill, int stroke, int radius) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(fill);
        d.setCornerRadius(dp(radius));
        d.setStroke(dp(1), stroke);
        return d;
    }

    private TextView text(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        return t;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }
}
