package org.michaeltv.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import androidx.core.app.NotificationCompat;

/**
 * Keeps the phone-side DVR relay alive while casting.
 *
 * The relay lives in the Chaquopy Python process and downloads the
 * provider's live HLS segments.  A Cast session is owned by the receiver,
 * but the SEGMENTS come from this phone, so the phone must not be
 * suspended when its screen turns off.  A mediaPlayback foreground service
 * plus a partial wake lock is the Android-sanctioned way to do that.
 *
 * The service deliberately does not own the relay: the relay is started by
 * the bridge before PlayerActivity opens and stopped by PlayerActivity on
 * teardown (or by the next xt_live_start).  This service only keeps the
 * process runnable and shows the required notification.
 *
 * Android 14+ requires the typed FGS permission declared in the manifest;
 * Android 13+ needs POST_NOTIFICATIONS for the notification to be visible
 * (the service still runs if the user declines it).
 */
public class DvrRelayService extends Service {

    private static final String CHANNEL_ID = "mtp_cast_dvr";
    private static final int NOTIFICATION_ID = 0x4D5456;   // "MTV"
    private PowerManager.WakeLock wakeLock;

    @Override
    public void onCreate() {
        super.onCreate();
        try {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null && Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = new NotificationChannel(
                        CHANNEL_ID, "Casting DVR",
                        NotificationManager.IMPORTANCE_LOW);
                ch.setDescription(
                        "Keeps live TV buffering for the TV while casting");
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
            PowerManager pm = getSystemService(PowerManager.class);
            if (pm != null) {
                wakeLock = pm.newWakeLock(
                        PowerManager.PARTIAL_WAKE_LOCK, "MichaelTV:castDvr");
                wakeLock.setReferenceCounted(false);
                wakeLock.acquire();
            }
            Intent open = new Intent(this, MainActivity.class);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) {
                flags |= PendingIntent.FLAG_IMMUTABLE;
            }
            PendingIntent pi = PendingIntent.getActivity(this, 0, open, flags);
            Notification n = new NotificationCompat.Builder(this, CHANNEL_ID)
                    .setSmallIcon(R.drawable.ic_launcher)
                    .setContentTitle("MichaelTV")
                    .setContentText(
                            "Buffering live TV for casting \u2014 DVR active")
                    .setOngoing(true)
                    .setOnlyAlertOnce(true)
                    .setContentIntent(pi)
                    .setPriority(NotificationCompat.PRIORITY_LOW)
                    .build();
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFICATION_ID, n,
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
            } else {
                startForeground(NOTIFICATION_ID, n);
            }
        } catch (Throwable t) {
            // Broken GMS/ROM or a missing FGS type must never crash the
            // player; casting still works with the screen on.
            stopSelf();
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        } catch (Throwable ignored) {
            // teardown never blocks the process exit
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
