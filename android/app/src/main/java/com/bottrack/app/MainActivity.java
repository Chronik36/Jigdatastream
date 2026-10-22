package com.bottrack.app;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * BotTrack for Android: the phone pages served by BotTrack on the tester's own Mac, in an app.
 * The first time it opens, it asks for the address shown on the Mac's Setup page, Phone access.
 * Links to anywhere else (carrier tracking pages) open in the browser.
 */
public class MainActivity extends Activity {
    private static final int NAVY = Color.parseColor("#132048");
    private static final int SURFACE = Color.parseColor("#111c46");
    private static final int TEXT = Color.parseColor("#e8eeff");
    private static final int MUTED = Color.parseColor("#8e9bc6");
    private static final int ACCENT = Color.parseColor("#3dd8ef");

    private SharedPreferences prefs;
    private WebView web;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = getSharedPreferences("bottrack", MODE_PRIVATE);
        String address = prefs.getString("address", "");
        if (address.isEmpty()) {
            showSetup("", null);
        } else {
            showWeb(address);
        }
    }

    /** Turn what the tester typed into the /m/ address: "100.1.2.3" becomes "http://100.1.2.3:8765/m/". */
    static String normalize(String typed) {
        String s = typed.trim();
        if (s.isEmpty()) return "";
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://" + s;
        Uri u = Uri.parse(s);
        if (u.getHost() == null || u.getHost().isEmpty()) return "";
        String port = u.getPort() == -1 && "http".equals(u.getScheme()) ? ":8765" : (u.getPort() == -1 ? "" : ":" + u.getPort());
        return u.getScheme() + "://" + u.getHost() + port + "/m/";
    }

    private void showSetup(String current, String problem) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackgroundColor(NAVY);
        int pad = dp(24);
        box.setPadding(pad, dp(72), pad, pad);

        TextView title = text("BotTrack", 30, TEXT);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        box.addView(title);
        TextView help = text("On your Mac, open BotTrack, go to Setup, Phone access, and type the address it shows. "
                + "Your phone and Mac both need the free Tailscale app, signed in to the same account.", 15, MUTED);
        help.setPadding(0, dp(10), 0, dp(20));
        box.addView(help);
        if (problem != null) {
            TextView err = text(problem, 15, Color.parseColor("#ff6b7a"));
            err.setPadding(0, 0, 0, dp(16));
            box.addView(err);
        }

        EditText input = new EditText(this);
        input.setHint("http://100.x.x.x:8765/m/");
        input.setText(current);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setTextColor(TEXT);
        input.setHintTextColor(MUTED);
        input.setBackgroundColor(SURFACE);
        input.setPadding(dp(14), dp(14), dp(14), dp(14));
        box.addView(input, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Button go = new Button(this);
        go.setText("Connect");
        go.setAllCaps(false);
        go.setTextColor(Color.parseColor("#04122a"));
        go.setBackgroundColor(ACCENT);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        lp.topMargin = dp(16);
        box.addView(go, lp);
        go.setOnClickListener(v -> {
            String address = normalize(input.getText().toString());
            if (address.isEmpty()) {
                input.setError("Type the address from BotTrack's Setup page");
                return;
            }
            prefs.edit().putString("address", address).apply();
            showWeb(address);
        });
        web = null;
        setContentView(box);
    }

    private void showWeb(String address) {
        final Uri home = Uri.parse(address);
        web = new WebView(this);
        web.setBackgroundColor(NAVY);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);  // confirm() before deletes
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSaveFormData(false);  // profile fields hold personal details
        s.setUserAgentString(s.getUserAgentString() + " BotTrackApp");  // shows the "Change Mac address" link
        CookieManager.getInstance().setAcceptCookie(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if ("bottrack".equals(u.getScheme())) {
                    showSetup(address, null);
                    return true;
                }
                if (sameServer(u, home)) return false;
                startActivity(new Intent(Intent.ACTION_VIEW, u));  // tracking pages and other sites
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError error) {
                if (req.isForMainFrame()) {
                    showSetup(address, "Couldn't reach BotTrack at that address. Check that BotTrack is open on your Mac, "
                            + "phone access is on, and Tailscale is connected on both.");
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();  // stay signed in after the app is closed
            }
        });
        setContentView(web);
        web.loadUrl(address);
    }

    static boolean sameServer(Uri a, Uri b) {
        return a.getHost() != null && a.getHost().equalsIgnoreCase(b.getHost()) && a.getPort() == b.getPort()
                && String.valueOf(a.getScheme()).equals(b.getScheme());
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else if (web == null && !prefs.getString("address", "").isEmpty()) {
            showWeb(prefs.getString("address", ""));
        } else {
            super.onBackPressed();
        }
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
