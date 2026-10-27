package com.bottrack.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Shipping alerts within seconds: keeps a connection open to the Mac's private alerts topic on ntfy.sh
 * (free, no account) and shows each order update the Mac posts there (shipped, out for delivery,
 * delivered, running late, cancelled) as a notification. Tapping one opens that order on Shipping.
 * Android needs a small "Watching for shipping updates" note to keep it running; it can be hidden
 * from Android's notification settings without stopping the alerts.
 */
public class AlertService extends Service {
    static final String ALERTS = "alerts";
    static final String WATCH = "watch";
    private static final int WATCH_ID = 1;
    private volatile boolean running;
    private Thread worker;

    /** Start watching if this phone is paired; safe to call any number of times. */
    static void start(Context c) {
        SharedPreferences p = c.getSharedPreferences("bottrack", MODE_PRIVATE);
        if (p.getString("topic", "").isEmpty() || !p.getBoolean("alerts", true)) return;
        Intent i = new Intent(c, AlertService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
            else c.startService(i);
        } catch (Exception ignored) {
            // Android refuses to start it from the background on some versions; the app starts it when opened.
        }
    }

    static void channels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        NotificationChannel alerts = new NotificationChannel(ALERTS, "Shipping updates", NotificationManager.IMPORTANCE_HIGH);
        alerts.setDescription("An order shipped, is out for delivery, was delivered, is running late or was cancelled");
        nm.createNotificationChannel(alerts);
        NotificationChannel watch = new NotificationChannel(WATCH, "Watching for updates", NotificationManager.IMPORTANCE_MIN);
        watch.setDescription("Keeps shipping alerts coming. You can hide this one.");
        watch.setShowBadge(false);
        nm.createNotificationChannel(watch);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        channels(this);
        Notification note = builder(WATCH)
                .setContentTitle("Watching for shipping updates")
                .setContentText("BotTrack tells you when an order ships or arrives")
                .setContentIntent(open(null, 0))
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 29) startForeground(WATCH_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        else startForeground(WATCH_ID, note);
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::listen, "bottrack-alerts");
            worker.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void listen() {
        SharedPreferences p = getSharedPreferences("bottrack", MODE_PRIVATE);
        long wait = 2000;
        while (running) {
            String topic = p.getString("topic", "");
            if (topic.isEmpty()) break;
            // since= the last alert seen, so ones posted while the phone was offline still show (ntfy keeps 12 hours).
            String since = p.getString("alert_since", "");
            HttpURLConnection c = null;
            try {
                String url = "https://ntfy.sh/" + Uri.encode(topic + "-alerts") + "/json"
                        + (since.isEmpty() ? "" : "?since=" + Uri.encode(since));
                c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(90000);  // ntfy sends a keepalive every 45 seconds
                try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8))) {
                    wait = 2000;
                    for (String line; running && (line = r.readLine()) != null; ) {
                        JSONObject m = new JSONObject(line);
                        if (!"message".equals(m.optString("event"))) continue;
                        p.edit().putString("alert_since", m.optString("id")).apply();
                        show(m);
                    }
                }
            } catch (Exception e) {
                // No internet or the connection dropped: try again, backing off up to five minutes.
            } finally {
                if (c != null) c.disconnect();
            }
            if (p.getString("alert_since", "").isEmpty()) {
                p.edit().putString("alert_since", String.valueOf(System.currentTimeMillis() / 1000)).apply();
            }
            try {
                Thread.sleep(wait);
            } catch (InterruptedException e) {
                break;
            }
            wait = Math.min(wait * 2, 300000);
        }
    }

    private void show(JSONObject m) {
        String path = "tracking?base=all";
        JSONArray tags = m.optJSONArray("tags");
        for (int i = 0; tags != null && i < tags.length(); i++) {
            String t = tags.optString(i);
            if (t.startsWith("order-")) path += "#" + t;
        }
        int id = (int) (m.optLong("time", System.currentTimeMillis() / 1000) % 1000000) + 10;
        Notification n = builder(ALERTS)
                .setContentTitle(m.optString("title", "BotTrack"))
                .setContentText(m.optString("message", ""))
                .setStyle(new Notification.BigTextStyle().bigText(m.optString("message", "")))
                .setContentIntent(open(path, id))
                .setAutoCancel(true)
                .build();
        getSystemService(NotificationManager.class).notify(id, n);
    }

    private Notification.Builder builder(String channel) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, channel) : new Notification.Builder(this);
        return b.setSmallIcon(R.drawable.ic_notify).setColor(0xff3dd8ef);
    }

    private PendingIntent open(String path, int code) {
        Intent i = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        if (path != null) i.putExtra("open", path);
        return PendingIntent.getActivity(this, code, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }
}
