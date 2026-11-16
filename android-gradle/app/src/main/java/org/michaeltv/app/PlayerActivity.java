package org.michaeltv.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
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
import androidx.media3.cast.CastPlayer;
import androidx.media3.cast.SessionAvailabilityListener;
import androidx.media3.common.AudioAttributes;
import androidx.media3.common.C;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MediaMetadata;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.exoplayer.ExoPlayer;
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
 * Fallback ladder on a playback error (each step is tried once):
 *   1. the primary URL (HLS for live channels)
 *   2. the alternate URL (progressive .ts for live channels)
 *   3. the last URL with the audio track disabled — keeps the PICTURE for
 *      files whose audio codec (AC3/DTS) the phone has no decoder for
 *   4. an error panel: Retry / Open in external player / Back
 *
 * Chromecast: when Google Play Services is available, a CastButton sits in
 * the top bar; picking a TV hands the current stream to the receiver (which
 * decodes AC3/DTS natively, so "video-only" streams regain audio) and the
 * phone becomes the remote.  When the session ends, playback returns to
 * the phone at the same position.  Cast is strictly optional — on devices
 * without cast-capable GMS (emulators, de-Googled ROMs) every cast touch-
 * point is guarded and local playback proceeds exactly as before.
 */
@OptIn(markerClass = UnstableApi.class)
public class PlayerActivity extends Activity {

    public static final String EXTRA_URL = "url";
    public static final String EXTRA_ALT_URL = "alt_url";
    public static final String EXTRA_TITLE = "title";

    private ExoPlayer player;
    private PlayerView playerView;
    private CastContext castContext;  // null = device cannot cast
    private CastPlayer castPlayer;
    private FrameLayout root;
    private LinearLayout topBar;
    private LinearLayout errorPanel;
    private TextView errorText;

    private String primaryUrl = "";
    private String altUrl = "";
    private String title = "";
    private String currentUrl = "";

    // 0 = primary, 1 = alternate URL, 2 = audio disabled
    private int attempt = 0;
    private boolean videoOnly = false;
    private boolean casting = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        Intent intent = getIntent();
        primaryUrl = safe(intent.getStringExtra(EXTRA_URL));
        altUrl = safe(intent.getStringExtra(EXTRA_ALT_URL));
        title = safe(intent.getStringExtra(EXTRA_TITLE));
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

        root.addView(buildTopBar());
        root.addView(buildErrorPanel());

        setContentView(root);

        player = new ExoPlayer.Builder(this).build();
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
        });
        playerView.setPlayer(player);

        // Show our own top bar together with the transport controls (the
        // default controller auto-hides after a few seconds).  The cast is
        // needed: PlayerView has two overloaded visibility listeners.
        playerView.setControllerVisibilityListener(
                (PlayerView.ControllerVisibilityListener) visibility ->
                        topBar.setVisibility(visibility));

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
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT);
        lp.gravity = Gravity.TOP;
        topBar.setLayoutParams(lp);

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

        // Cast entry point (media3 1.2.1 has no CastButton yet — the
        // classic MediaRouteButton + CastButtonFactory wiring drives the
        // same CastPlayer session).  Only added when the cast context
        // probe succeeded; setUpMediaRouteButton can still throw on
        // broken GMS images, so keep it guarded either way.
        if (castContext != null) {
            try {
                MediaRouteButton castButton = new MediaRouteButton(this);
                CastButtonFactory.setUpMediaRouteButton(this, castButton);
                castButton.setPadding(dp(6), 0, dp(6), 0);
                LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.WRAP_CONTENT,
                        LinearLayout.LayoutParams.MATCH_PARENT);
                clp.gravity = Gravity.CENTER_VERTICAL;
                topBar.addView(castButton, clp);
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

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    // ---------------------------------------------------------- playback

    private void play(String url) {
        currentUrl = url;
        errorPanel.setVisibility(View.GONE);
        if (casting && castPlayer != null) {
            castPlayer.setMediaItem(castMediaItem(url));
            castPlayer.prepare();
            castPlayer.setPlayWhenReady(true);
        } else {
            player.setMediaItem(MediaItem.fromUri(url));
            player.prepare();
            player.setPlayWhenReady(true);
        }
    }

    private void retryFromStart() {
        attempt = 0;
        videoOnly = false;
        player.setTrackSelectionParameters(player.getTrackSelectionParameters()
                .buildUpon()
                .setTrackTypeDisabled(C.TRACK_TYPE_AUDIO, false)
                .build());
        play(primaryUrl);
    }

    private void handlePlaybackError(PlaybackException error) {
        // 1 -> 2: live channels have a progressive .ts fallback URL
        if (attempt == 0 && !altUrl.isEmpty()) {
            attempt = 1;
            toast("Trying alternate stream\u2026");
            play(altUrl);
            return;
        }
        // 2 -> 3: keep the picture when the phone lacks the audio codec
        // (AC3/EAC3/DTS are common in debrid MKV files).  Only codec
        // failures qualify — a network/parse failure must not silently
        // mute a stream that would have played fine.
        if (!videoOnly && looksLikeCodecFailure(error)) {
            videoOnly = true;
            attempt = 2;
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
        }
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
                    // No .ts/video-only ladder on the receiver — the TV has
                    // its own decoders; just surface the panel.
                    showError(error);
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
        if (casting || castPlayer == null || player == null || playerView == null) return;
        casting = true;
        // Hand the position over for VOD; a live stream is restarted at
        // the head of its window (seeking a fresh live position on the
        // receiver can land outside the DVR window and error out).
        boolean live = player.isCurrentMediaItemLive();
        long pos = live ? 0 : player.getCurrentPosition();
        player.pause();
        playerView.setPlayer(castPlayer);
        castPlayer.setMediaItem(castMediaItem(currentUrl));
        castPlayer.prepare();
        if (pos > 0) castPlayer.seekTo(pos);
        castPlayer.setPlayWhenReady(true);
        toast("Playing on TV");
    }

    private void switchToLocal() {
        if (!casting || player == null || playerView == null) return;
        casting = false;
        long pos = castPlayer != null ? castPlayer.getCurrentPosition() : 0;
        playerView.setPlayer(player);
        player.setMediaItem(MediaItem.fromUri(currentUrl));
        if (pos > 0) player.seekTo(pos);
        player.prepare();
        player.setPlayWhenReady(true);
        toast("Back on phone");
    }

    // ----------------------------------------------------------- lifecycle

    @Override
    protected void onStop() {
        // Leaving the screen (home button, external player) stops the
        // audio instead of continuing in the background — unless the TV is
        // playing: the receiver keeps going and the phone stays the remote.
        if (!casting && player != null) player.pause();
        super.onStop();
    }

    @Override
    protected void onDestroy() {
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
