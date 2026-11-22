package org.michaeltv.app;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.OptIn;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.media3.cast.CastPlayer;
import androidx.media3.cast.SessionAvailabilityListener;
import androidx.media3.common.AudioAttributes;
import androidx.media3.common.C;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MediaMetadata;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.exoplayer.DefaultLoadControl;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory;
import androidx.media3.exoplayer.upstream.DefaultLoadErrorHandlingPolicy;
import androidx.media3.ui.PlayerView;
import androidx.mediarouter.app.MediaRouteButton;

import com.google.android.gms.cast.framework.CastButtonFactory;
import com.google.android.gms.cast.framework.CastContext;

/**
 * The native playback screen (ExoPlayer/media3).
 *
 * Why this exists: the old player was a WebView {@code <video>} element,
 * and Android's WebView cannot decode what IPTV/debrid streams actually
 * are — MPEG-TS live channels and MKV movie files.  Every stream therefore
 * needed VLC.  ExoPlayer plays MPEG-TS, HLS (m3u8), MKV, MP4 and WebM in
 * the app itself.
 *
 * Live channels are opened on the phone-side DVR relay (live_relay.py):
 * the provider's original HLS segments are buffered locally and served as
 * a seekable live playlist, so the transport row below can rewind/jump
 * within the configured window.  The same playlist is handed to a Cast
 * receiver, which is why casting keeps the DVR and full quality (the
 * segments are never decoded/transcoded on the phone).
 *
 * Fallback ladder on a playback error (each step is tried once):
 *   1. the primary URL (the DVR relay for live, HLS otherwise)
 *   2. the direct provider HLS (live only; no DVR)
 *   3. the alternate URL (progressive .ts for live channels)
 *   4. the last URL with the audio track disabled — keeps the PICTURE for
 *      files whose audio codec (AC3/DTS) the phone has no decoder for
 *   5. an error panel: Retry / Open in external player / Back
 *
 * Chromecast: CastOptionsProvider + the manifest metadata make
 * CastContext usable; this Activity is an AppCompatActivity so the
 * MediaRouteButton device chooser (an AppCompat dialog) can open.  The
 * receiver plays the relay's HLS playlist at the original quality and the
 * phone stays the remote; if the TV cannot reach the phone (AP isolation),
 * casting falls back to the direct provider HLS.
 */
@OptIn(markerClass = UnstableApi.class)
public class PlayerActivity extends AppCompatActivity {

    public static final String EXTRA_URL = "url";           // phone source
    public static final String EXTRA_ALT_URL = "alt_url";   // direct .ts
    public static final String EXTRA_TITLE = "title";
    public static final String EXTRA_LIVE = "live";
    public static final String EXTRA_CAST_URL = "cast_url";     // LAN relay
    public static final String EXTRA_DIRECT_URL = "direct_url";  // provider HLS
    public static final String EXTRA_CAST_DVR = "cast_dvr";
    public static final String EXTRA_CAST_BG = "cast_bg";
    public static final String EXTRA_RELAY_SESSION = "relay_session";

    private ExoPlayer player;
    private PlayerView playerView;
    private CastContext castContext;  // null = device cannot cast
    private CastPlayer castPlayer;
    private FrameLayout root;
    private LinearLayout chrome;
    private LinearLayout topBar;
    private LinearLayout controlsRow;
    private LinearLayout errorPanel;
    private TextView errorText;
    private TextView dvrLabel;
    private Button btnPlayPause;
    private Button btnLive;

    private String primaryUrl = "";
    private String altUrl = "";
    private String directUrl = "";
    private String castUrl = "";
    private String relaySession = "";
    private String title = "";
    private String currentUrl = "";
    private String localUrl = "";      // what the PHONE should play
    private boolean live = false;
    private boolean castWithDvr = true;
    private boolean castBgBuffer = true;

    // 0 = primary, 1 = direct HLS, 2 = alternate .ts, 3 = audio disabled
    private int attempt = 0;
    private boolean videoOnly = false;
    private boolean casting = false;
    private boolean castFallbackTried = false;
    private boolean castUsedRelay = false;   // cast timeline == relay timeline
    private boolean dvrServiceOn = false;

    // Live streams are opened with a two-batch cushion in ExoPlayer's
    // load control: the panel pushes each 5 s of MPEG-TS as one burst and
    // then goes silent for 5 s (measured 2026-11-22), so a player that
    // starts on the first 2.5 s of data starves at the first seam. The
    // deeper start gate costs ~10 s of spinner once and rides the cadence.
    private static final int LIVE_MIN_BUFFER_MS = 50_000;   // keep 50 s
    private static final int LIVE_MAX_BUFFER_MS = 60_000;
    private static final int LIVE_START_MS = 10_000;        // two batches
    private static final int LIVE_REBUFFER_MS = 15_000;
    private static final int LIVE_RETRIES = 8;
    private static final int LIVE_RECONNECT_MAX = 6;        // within the window
    private static final long LIVE_STABLE_MS = 60_000;      // resets the count
    // The HLS DVR playlist is live; 10 s behind its edge matches the
    // Windows cushion and leaves the whole buffered window to rewind into.
    private static final long LIVE_TARGET_OFFSET_MS = 10_000L;

    private boolean handingOff = false;
    private int liveReconnects = 0;
    private long lastReadyAt = 0L;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable transportTick = new Runnable() {
        @Override
        public void run() {
            updateTransportUi();
            ui.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        Intent intent = getIntent();
        primaryUrl = safe(intent.getStringExtra(EXTRA_URL));
        altUrl = safe(intent.getStringExtra(EXTRA_ALT_URL));
        directUrl = safe(intent.getStringExtra(EXTRA_DIRECT_URL));
        castUrl = safe(intent.getStringExtra(EXTRA_CAST_URL));
        relaySession = safe(intent.getStringExtra(EXTRA_RELAY_SESSION));
        title = safe(intent.getStringExtra(EXTRA_TITLE));
        live = intent.getBooleanExtra(EXTRA_LIVE, false);
        castWithDvr = intent.getBooleanExtra(EXTRA_CAST_DVR, true);
        castBgBuffer = intent.getBooleanExtra(EXTRA_CAST_BG, true);
        localUrl = primaryUrl;
        if (TextUtils.isEmpty(primaryUrl)) {
            finish();
            return;
        }

        // Probed before the top bar is built so the cast button is only
        // ever added on devices that can actually cast.
        castContext = peekCastContext(this);

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        playerView = new PlayerView(this);
        playerView.setBackgroundColor(Color.BLACK);
        playerView.setLayoutParams(new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        root.addView(playerView);

        chrome = new LinearLayout(this);
        chrome.setOrientation(LinearLayout.VERTICAL);
        FrameLayout.LayoutParams clp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT);
        clp.gravity = Gravity.TOP;
        chrome.setLayoutParams(clp);
        chrome.addView(buildTopBar(), new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        chrome.addView(buildControlsRow(), new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        root.addView(chrome);
        root.addView(buildErrorPanel());

        setContentView(root);

        // Live gets the two-batch cushion (see LIVE_START_MS); VOD keeps
        // the stock start gate so a movie does not wait for 10 s.
        DefaultLoadControl loadControl = new DefaultLoadControl.Builder()
                .setBufferDurationsMs(
                        LIVE_MIN_BUFFER_MS,
                        live ? LIVE_MAX_BUFFER_MS : LIVE_MIN_BUFFER_MS,
                        live ? LIVE_START_MS : 2_500,
                        live ? LIVE_REBUFFER_MS : 5_000)
                .build();
        player = new ExoPlayer.Builder(this)
                .setLoadControl(loadControl)
                // A live provider drops/ends bodies more often than a VOD
                // CDN; the default 3 retries give up while the stream is
                // only having a bad batch. media3 1.2.1 puts this on the
                // media-source factory, not the player builder.
                .setMediaSourceFactory(new DefaultMediaSourceFactory(this)
                        .setLoadErrorHandlingPolicy(
                                new DefaultLoadErrorHandlingPolicy(
                                        live ? LIVE_RETRIES : 3)))
                .build();
        player.setAudioAttributes(new AudioAttributes.Builder()
                .setContentType(C.AUDIO_CONTENT_TYPE_MOVIE)
                .setUsage(C.USAGE_MEDIA)
                .build(), true);
        player.setHandleAudioBecomingNoisy(true);
        player.addListener(new Player.Listener() {
            @Override
            public void onPlayerError(PlaybackException error) {
                handlePlaybackError(error);
            }

            @Override
            public void onPlaybackStateChanged(int state) {
                if (state == Player.STATE_READY) {
                    lastReadyAt = SystemClock.elapsedRealtime();
                } else if (state == Player.STATE_ENDED && live
                        && !casting && !handingOff) {
                    onLiveEnded();
                }
            }
        });
        playerView.setPlayer(player);

        // Show our own top bar together with the transport controls (the
        // default controller auto-hides after a few seconds).  The cast is
        // needed: PlayerView has two overloaded visibility listeners.
        playerView.setControllerVisibilityListener(
                (PlayerView.ControllerVisibilityListener) visibility ->
                        chrome.setVisibility(visibility));

        setupCast();
        play(primaryUrl);
    }

    private static String safe(String s) {
        return s == null ? "" : s;
    }

    // ---------------------------------------------------------------- UI

    private View buildTopBar() {
        topBar = new LinearLayout(this);
        topBar.setOrientation(LinearLayout.HORIZONTAL);
        topBar.setGravity(Gravity.CENTER_VERTICAL);
        topBar.setPadding(dp(8), dp(6), dp(8), dp(6));
        topBar.setBackgroundColor(0xCC000000);

        Button back = makeButton("\u25C0");
        back.setOnClickListener(v -> finish());
        topBar.addView(back, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        TextView t = new TextView(this);
        t.setText(title);
        t.setTextColor(Color.WHITE);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        t.setSingleLine(true);
        t.setEllipsize(TextUtils.TruncateAt.END);
        t.setPadding(dp(10), 0, dp(10), 0);
        topBar.addView(t, new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        // Cast entry point.  Only added when CastContext initialized; the
        // SDK's device chooser needs this AppCompat host (see the class
        // doc) and setUpMediaRouteButton can still throw on broken GMS
        // images, so keep it guarded either way.
        if (castContext != null) {
            try {
                MediaRouteButton castButton = new MediaRouteButton(this);
                CastButtonFactory.setUpMediaRouteButton(this, castButton);
                castButton.setPadding(dp(6), 0, dp(6), 0);
                LinearLayout.LayoutParams cbp = new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.WRAP_CONTENT,
                        LinearLayout.LayoutParams.MATCH_PARENT);
                cbp.gravity = Gravity.CENTER_VERTICAL;
                topBar.addView(castButton, cbp);
            } catch (Throwable ignored) {
                // No cast UI on this device.
            }
        }

        Button ext = makeButton("Open in external player");
        ext.setOnClickListener(v -> openExternal(currentUrl));
        topBar.addView(ext, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        return topBar;
    }

    /**
     * The DVR transport row.  It acts on whichever player currently owns
     * playback — the local ExoPlayer or the CastPlayer — so the same
     * buttons rewind the TV stream while casting.  The PlayerView's own
     * seek bar is still there; these are the large, always-visible jumps.
     */
    private View buildControlsRow() {
        controlsRow = new LinearLayout(this);
        controlsRow.setOrientation(LinearLayout.HORIZONTAL);
        controlsRow.setGravity(Gravity.CENTER_VERTICAL);
        controlsRow.setPadding(dp(6), dp(2), dp(6), dp(2));
        controlsRow.setBackgroundColor(0xB3000000);

        Button r60 = makeSmallButton("\u00AB60");
        r60.setOnClickListener(v -> seekBy(-60_000L));
        Button r10 = makeSmallButton("\u00AB10");
        r10.setOnClickListener(v -> seekBy(-10_000L));
        Button f10 = makeSmallButton("10\u00BB");
        f10.setOnClickListener(v -> seekBy(10_000L));
        btnLive = makeSmallButton("LIVE");
        btnLive.setOnClickListener(v -> goLive());
        btnPlayPause = makeSmallButton("Pause");
        btnPlayPause.setOnClickListener(v -> togglePlay());

        controlsRow.addView(r60, controlParams());
        controlsRow.addView(r10, controlParams());
        controlsRow.addView(f10, controlParams());
        controlsRow.addView(btnLive, controlParams());
        controlsRow.addView(btnPlayPause, controlParams());

        dvrLabel = new TextView(this);
        dvrLabel.setTextColor(0xFFD0D0D8);
        dvrLabel.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        dvrLabel.setSingleLine(true);
        dvrLabel.setEllipsize(TextUtils.TruncateAt.END);
        dvrLabel.setGravity(Gravity.END | Gravity.CENTER_VERTICAL);
        dvrLabel.setPadding(dp(6), 0, dp(2), 0);
        controlsRow.addView(dvrLabel, new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        return controlsRow;
    }

    private LinearLayout.LayoutParams controlParams() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(dp(2), 0, dp(2), 0);
        return lp;
    }

    private View buildErrorPanel() {
        errorPanel = new LinearLayout(this);
        errorPanel.setOrientation(LinearLayout.VERTICAL);
        errorPanel.setGravity(Gravity.CENTER);
        errorPanel.setPadding(dp(24), dp(24), dp(24), dp(24));
        errorPanel.setBackgroundColor(0xF2000000);
        errorPanel.setVisibility(View.GONE);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT);
        errorPanel.setLayoutParams(lp);

        TextView head = new TextView(this);
        head.setText("This stream can't play here");
        head.setTextColor(Color.WHITE);
        head.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        head.setGravity(Gravity.CENTER);
        errorPanel.addView(head);

        errorText = new TextView(this);
        errorText.setTextColor(0xFFB0B0BC);
        errorText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        errorText.setGravity(Gravity.CENTER);
        errorText.setPadding(0, dp(10), 0, dp(18));
        errorPanel.addView(errorText);

        Button retry = makeButton("Retry");
        retry.setOnClickListener(v -> retryFromStart());
        errorPanel.addView(retry, errorButtonParams());

        Button ext = makeButton("Open in external player");
        ext.setOnClickListener(v -> openExternal(currentUrl));
        errorPanel.addView(ext, errorButtonParams());

        Button back = makeButton("Back");
        back.setOnClickListener(v -> finish());
        errorPanel.addView(back, errorButtonParams());
        return errorPanel;
    }

    private LinearLayout.LayoutParams errorButtonParams() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(8);
        return lp;
    }

    private Button makeButton(String label) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        return b;
    }

    private Button makeSmallButton(String label) {
        Button b = makeButton(label);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        b.setPadding(dp(6), 0, dp(6), 0);
        b.setMinWidth(0);
        b.setMinimumWidth(0);
        return b;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    // -------------------------------------------------------- transport

    private Player activePlayer() {
        if (casting && castPlayer != null) {
            return castPlayer;
        }
        return player;
    }

    private void seekBy(long deltaMs) {
        Player p = activePlayer();
        if (p == null) return;
        long pos = Math.max(0L, p.getCurrentPosition());
        long target = Math.max(0L, pos + deltaMs);
        long dur = p.getDuration();
        if (dur != C.TIME_UNSET && dur > 0
                && !p.isCurrentMediaItemLive()) {
            target = Math.min(target, dur);
        }
        p.seekTo(target);
    }

    private void goLive() {
        Player p = activePlayer();
        if (p == null) return;
        if (p.isCurrentMediaItemLive()) {
            // The default live position honours the 10 s target offset and
            // is always inside the receiver's/player's seekable window.
            p.seekToDefaultPosition();
            p.setPlayWhenReady(true);
        } else if (p.getDuration() != C.TIME_UNSET && p.getDuration() > 0) {
            p.seekTo(p.getDuration());
        }
    }

    private void togglePlay() {
        Player p = activePlayer();
        if (p != null) {
            p.setPlayWhenReady(!p.getPlayWhenReady());
        }
    }

    private void updateTransportUi() {
        Player p = activePlayer();
        if (p == null || btnPlayPause == null || dvrLabel == null) return;
        btnPlayPause.setText(p.getPlayWhenReady() ? "Pause" : "Play");
        boolean isLive = p.isCurrentMediaItemLive();
        if (btnLive != null) btnLive.setEnabled(isLive || live);
        long pos = Math.max(0L, p.getCurrentPosition());
        long dur = p.getDuration();
        if (dur != C.TIME_UNSET && dur > 0) {
            dvrLabel.setText((isLive ? "DVR " : "")
                    + fmtMs(pos) + " / " + fmtMs(dur));
        } else if (live) {
            dvrLabel.setText("DVR " + fmtMs(pos));
        } else {
            dvrLabel.setText("");
        }
    }

    private static String fmtMs(long ms) {
        long total = Math.max(0L, ms) / 1000L;
        long h = total / 3600L;
        long m = (total % 3600L) / 60L;
        long s = total % 60L;
        if (h > 0) {
            return String.format("%d:%02d:%02d", h, m, s);
        }
        return String.format("%d:%02d", m, s);
    }

    // ---------------------------------------------------------- playback

    private MediaItem mediaItem(String url) {
        MediaItem.Builder b = new MediaItem.Builder().setUri(url);
        if (live) {
            // Matches the Windows cushion; the relay playlist keeps the
            // whole configured window behind this position.
            b.setLiveConfiguration(new MediaItem.LiveConfiguration.Builder()
                    .setTargetOffsetMs(LIVE_TARGET_OFFSET_MS)
                    .build());
        }
        return b.build();
    }

    private void play(String url) {
        currentUrl = url;
        errorPanel.setVisibility(View.GONE);
        if (casting && castPlayer != null) {
            castPlayer.setMediaItem(castMediaItem(url));
            castPlayer.prepare();
            castPlayer.setPlayWhenReady(true);
        } else {
            localUrl = url;
            player.setMediaItem(mediaItem(url));
            player.prepare();
            player.setPlayWhenReady(true);
        }
        updateTransportUi();
    }

    private void retryFromStart() {
        attempt = 0;
        videoOnly = false;
        castFallbackTried = false;
        player.setTrackSelectionParameters(player.getTrackSelectionParameters()
                .buildUpon()
                .setTrackTypeDisabled(C.TRACK_TYPE_AUDIO, false)
                .build());
        play(primaryUrl);
    }

    private void handlePlaybackError(PlaybackException error) {
        // 1 -> 2: the relay failed; try the provider HLS directly (live
        // picture, but no DVR window on this device).
        if (attempt == 0 && !directUrl.isEmpty()
                && !directUrl.equals(currentUrl)) {
            attempt = 1;
            toast("DVR buffer failed \u2014 playing the direct feed");
            play(directUrl);
            return;
        }
        // 2 -> 3: live channels have a progressive .ts fallback URL
        if (attempt <= 1 && !altUrl.isEmpty()
                && !altUrl.equals(currentUrl)) {
            attempt = 2;
            toast("Trying alternate stream\u2026");
            play(altUrl);
            return;
        }
        // 3 -> 4: keep the picture when the phone lacks the audio codec
        // (AC3/EAC3/DTS are common in debrid MKV files).  Only codec
        // failures qualify — a network/parse failure must not silently
        // mute a stream that would have played fine.
        if (!videoOnly && looksLikeCodecFailure(error)) {
            videoOnly = true;
            attempt = 3;
            player.setTrackSelectionParameters(player.getTrackSelectionParameters()
                    .buildUpon()
                    .setTrackTypeDisabled(C.TRACK_TYPE_AUDIO, true)
                    .build());
            toast("Audio format not supported \u2014 playing video muted");
            play(currentUrl);
            return;
        }
        showError(error);
    }

    private static boolean looksLikeCodecFailure(PlaybackException error) {
        if (error == null) return false;
        switch (error.errorCode) {
            case PlaybackException.ERROR_CODE_DECODER_INIT_FAILED:
            case PlaybackException.ERROR_CODE_DECODING_FAILED:
            case PlaybackException.ERROR_CODE_AUDIO_TRACK_INIT_FAILED:
            case PlaybackException.ERROR_CODE_AUDIO_TRACK_WRITE_FAILED:
                return true;
            default:
                return false;
        }
    }

    private void showError(PlaybackException error) {
        String detail = error == null ? "" : error.getErrorCodeName();
        errorText.setText("The player tried every format this stream offers." +
                (TextUtils.isEmpty(detail) ? "" : "\n\n" + detail));
        errorPanel.setVisibility(View.VISIBLE);
    }

    /**
     * A live body ended CLEANLY. Providers cap connection age / rebalance
     * the CDN, and ExoPlayer reads the clean EOF as end-of-media: no
     * onPlayerError, so the format ladder below never runs and the screen
     * sits black. The DVR relay re-resolves the provider every poll, so
     * this normally only fires if the relay itself stopped; try direct
     * HLS/.ts once, then reconnect the relay URL with the reconnect
     * budget. The count resets after a minute of real playback.
     */
    private void onLiveEnded() {
        long now = SystemClock.elapsedRealtime();
        if (lastReadyAt > 0 && now - lastReadyAt >= LIVE_STABLE_MS) {
            liveReconnects = 0;
        }
        if (liveReconnects >= LIVE_RECONNECT_MAX) {
            errorText.setText("This live stream keeps ending.\n\nThe "
                    + "provider is dropping the connection — try again "
                    + "later or open it in an external player.");
            errorPanel.setVisibility(View.VISIBLE);
            return;
        }
        liveReconnects++;
        if (attempt == 0 && !directUrl.isEmpty()
                && !directUrl.equals(currentUrl)) {
            attempt = 1;
            toast("Live stream ended \u2014 trying the direct feed\u2026");
            play(directUrl);
            return;
        }
        if (attempt <= 1 && !altUrl.isEmpty()
                && !altUrl.equals(currentUrl)) {
            attempt = 2;
            toast("Live stream ended \u2014 trying the alternate feed\u2026");
            play(altUrl);
            return;
        }
        toast("Reconnecting live stream\u2026");
        root.postDelayed(() -> {
            if (!isFinishing() && !isDestroyed() && live && player != null
                    && !casting && !handingOff) {
                play(currentUrl);
            }
        }, 1200);
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }

    private void openExternal(String url) {
        if (TextUtils.isEmpty(url)) return;
        try {
            Intent i = new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(Uri.parse(url), "video/*");
            startActivity(i);
        } catch (ActivityNotFoundException e) {
            toast("No external player installed");
            return;
        }
        // The external player gets the same URL.  For live that is the
        // local relay playlist, so VLC reads the phone's DVR (and the
        // provider still sees exactly ONE connection).  Release the local
        // ExoPlayer so it does not compete for the relay socket.
        handingOff = true;
        if (player != null) {
            try {
                player.pause();
                player.stop();
                player.clearMediaItems();
            } catch (Throwable ignored) {
                // player already released / activity tearing down
            }
        }
        toast("Paused here so the external player has the stream");
    }

    // --------------------------------------------------------------- cast

    /**
     * Wire up Chromecast when the device can do it.  Everything cast-
     * related is wrapped in catch-Throwable: on emulators and de-Googled
     * ROMs {@link CastContext#getSharedInstance} fails (a broken GMS image
     * can even throw from a missing class), and the only acceptable
     * fallback is local playback that behaves exactly as before.  Runs
     * AFTER the views/players exist because the availability listener may
     * fire its first callback immediately if a session is already live.
     */
    private static CastContext peekCastContext(Activity activity) {
        try {
            return CastContext.getSharedInstance(activity);
        } catch (Throwable t) {
            return null;  // no cast on this device — fine
        }
    }

    private void setupCast() {
        if (castContext == null) return;
        try {
            castPlayer = new CastPlayer(castContext);
            castPlayer.addListener(new Player.Listener() {
                @Override
                public void onPlayerError(PlaybackException error) {
                    handleCastError(error);
                }
            });
            castPlayer.setSessionAvailabilityListener(
                    new SessionAvailabilityListener() {
                        @Override
                        public void onCastSessionAvailable() {
                            switchToCast();
                        }

                        @Override
                        public void onCastSessionUnavailable() {
                            switchToLocal();
                        }
                    });
        } catch (Throwable t) {
            castPlayer = null;
        }
    }

    private String castTargetUrl() {
        // Full quality + DVR needs the TV to fetch the phone's relay.  If
        // the user disabled it or the phone has no LAN address, cast the
        // provider HLS directly (still no transcode, but no TV DVR).
        if (castWithDvr && !castUrl.isEmpty()) {
            return castUrl;
        }
        if (!directUrl.isEmpty()) {
            return directUrl;
        }
        return currentUrl;
    }

    private MediaItem castMediaItem(String url) {
        // The receiver shows the metadata on the "now playing" screen while
        // the phone is the remote.
        MediaMetadata metadata = new MediaMetadata.Builder()
                .setTitle(TextUtils.isEmpty(title) ? "MichaelTV" : title)
                .build();
        return new MediaItem.Builder()
                .setUri(Uri.parse(url))
                .setMediaMetadata(metadata)
                .build();
    }

    private void switchToCast() {
        if (casting || castPlayer == null || player == null
                || playerView == null) {
            return;
        }
        casting = true;
        long pos = Math.max(0L, player.getCurrentPosition());
        player.pause();
        playerView.setPlayer(castPlayer);
        String target = castTargetUrl();
        castPlayer.setMediaItem(castMediaItem(target));
        castPlayer.prepare();
        // The relay playlist is the SAME session/timeline on both devices,
        // so hand the DVR position over exactly.  A direct fallback has a
        // different timeline; let the receiver start at its live edge.
        if (pos > 0 && !castUrl.isEmpty() && target.equals(castUrl)) {
            castPlayer.seekTo(pos);
        }
        castPlayer.setPlayWhenReady(true);
        castFallbackTried = false;
        castUsedRelay = !castUrl.isEmpty() && target.equals(castUrl);
        maybeStartDvrService();
        toast(target.equals(castUrl)
                ? "Casting with DVR"
                : "Casting live \u2014 TV rewind unavailable");
    }

    private void switchToLocal() {
        if (!casting || player == null || playerView == null) return;
        casting = false;
        long pos = castPlayer != null ? castPlayer.getCurrentPosition() : 0L;
        stopDvrService();
        playerView.setPlayer(player);
        String target = TextUtils.isEmpty(localUrl) ? primaryUrl : localUrl;
        player.setMediaItem(mediaItem(target));
        // Only the relay timeline is shared with the receiver.  A direct
        // cast starts a different provider playlist; mapping its position
        // onto the relay would land minutes away, so start at the live
        // edge instead (the DVR window is still fully available).
        if (pos > 0 && castUsedRelay) player.seekTo(pos);
        castUsedRelay = false;
        player.prepare();
        player.setPlayWhenReady(true);
        toast("Back on phone");
    }

    /**
     * The TV could not fetch the phone's relay (wrong LAN, AP isolation,
     * receiver limits).  Cast the provider HLS directly instead of dying:
     * picture/audio stay full quality, but the TV's rewind window is the
     * provider's short live playlist.  The phone's own DVR (localUrl) is
     * untouched for switchToLocal.
     */
    private void handleCastError(PlaybackException error) {
        if (!castFallbackTried && !castUrl.isEmpty()
                && !directUrl.isEmpty()) {
            castFallbackTried = true;
            toast("TV can't reach the phone DVR \u2014 casting direct");
            play(directUrl);
            return;
        }
        showError(error);
    }

    private void maybeStartDvrService() {
        if (!live || !castWithDvr || castUrl.isEmpty()) return;
        if (Build.VERSION.SDK_INT >= 33
                && ContextCompat.checkSelfPermission(
                        this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            try {
                ActivityCompat.requestPermissions(this,
                        new String[]{Manifest.permission.POST_NOTIFICATIONS},
                        41);
            } catch (Throwable ignored) {
                // notification still optional
            }
        }
        try {
            ContextCompat.startForegroundService(
                    this, new Intent(this, DvrRelayService.class));
            dvrServiceOn = true;
        } catch (Throwable t) {
            toast("Background DVR unavailable");
        }
    }

    private void stopDvrService() {
        if (!dvrServiceOn) return;
        dvrServiceOn = false;
        try {
            stopService(new Intent(this, DvrRelayService.class));
        } catch (Throwable ignored) {
            // service already gone
        }
    }

    // ----------------------------------------------------------- lifecycle

    @Override
    protected void onResume() {
        super.onResume();
        // Returning from the external player: the local stream was closed
        // for the handoff (see openExternal), so pick it back up. VLC may
        // still be alive in the background; if it is, stopping it is the
        // user's business — but the app must not present a dead player.
        if (handingOff) {
            handingOff = false;
            if (player != null && !casting && player.getMediaItemCount() == 0
                    && !TextUtils.isEmpty(currentUrl)) {
                play(TextUtils.isEmpty(localUrl) ? currentUrl : localUrl);
            }
        }
        ui.removeCallbacks(transportTick);
        ui.post(transportTick);
    }

    @Override
    protected void onPause() {
        ui.removeCallbacks(transportTick);
        super.onPause();
    }

    @Override
    protected void onStop() {
        // Leaving the screen (home button, external player) stops the
        // audio instead of continuing in the background — unless the TV is
        // playing: the receiver keeps going and the phone stays the remote.
        if (!casting && player != null && !handingOff) player.pause();
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(transportTick);
        stopDvrService();
        // The relay owns the provider's single live connection and the
        // segment cache; leaving the player (or switching channels) must
        // release it.  The bridge's relay is a module singleton, so this
        // works even if MainActivity was recreated.
        if (live) {
            // Stop only THIS player's relay: a late teardown must not kill
            // the relay the next channel just started.  stop() joins the
            // relay threads for a moment, so never block the UI thread.
            final String stopArgs = TextUtils.isEmpty(relaySession)
                    ? "{}"
                    : "{\"session\":\"" + relaySession + "\"}";
            Thread stopper = new Thread(
                    () -> AppBridge.rpc("xt_live_stop", stopArgs),
                    "mtp-relay-stop");
            stopper.setDaemon(true);
            stopper.start();
        }
        if (castPlayer != null) {
            try {
                castPlayer.setSessionAvailabilityListener(null);
                castPlayer.release();
            } catch (Throwable t) {
                // Broken GMS can explode during teardown — never let it
                // take the activity down with it.
            }
            castPlayer = null;
        }
        if (player != null) {
            player.release();
            player = null;
        }
        if (playerView != null) {
            playerView.setPlayer(null);
            playerView = null;
        }
        super.onDestroy();
    }
}
