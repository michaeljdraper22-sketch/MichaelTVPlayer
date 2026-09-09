package org.michaeltv.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * The whole app: one Activity hosting a WebView (assets/www/index.html is
 * the UI) plus the Chaquopy Python runtime (bridge.py holds the
 * Stremio/Xtream logic, which is a verbatim copy of the desktop player's
 * src/ core).
 *
 * No androidx anywhere — plain framework classes only.
 */
public class MainActivity extends Activity {

    private WebView webView;
    private PyObject bridge;
    // Every Python call runs on this ONE plain worker thread instead of
    // the WebView's JavaBridge thread: field reports showed calls made
    // from the JavaBridge thread dying with a native-level Java exception
    // even though the Python side cannot raise (bridge.rpc catches
    // everything) — running on our own thread removes that whole class of
    // Chromium-thread-state interference, and the try/catch below turns
    // any survivor into a diagnostic {"error": ...} the UI can show.
    private final ExecutorService pyExecutor = Executors.newSingleThreadExecutor();

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Video watching app: never let the screen sleep mid-episode.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }
        bridge = Python.getInstance().getModule("bridge")
                .callAttr("Bridge", getFilesDir().getPath());

        webView = new WebView(this);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        // Autoplay the stream as soon as the player screen opens.
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(true);              // load the asset UI
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        webView.setBackgroundColor(0xFF000000);
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                // Keep every http(s) navigation inside the app — no
                // external browsing.
                return false;
            }
        });
        webView.addJavascriptInterface(new JavaBridge(), "py");
        setContentView(webView);
        webView.loadUrl("file:///android_asset/www/index.html");
    }

    /**
     * JS-facing interface. Every method here runs on the WebView's
     * "JavaBridge" background thread: the blocking rpc call is fine there
     * (and REQUIRED — network on the UI thread would throw), but nothing
     * in rpc may ever touch the UI.
     */
    private class JavaBridge {

        @JavascriptInterface
        public String rpc(final String cmd, final String argsJson) {
            Future<String> task = pyExecutor.submit(() ->
                    bridge.callAttr("rpc", cmd, argsJson).toString());
            try {
                return task.get(180, TimeUnit.SECONDS);
            } catch (Throwable t) {
                // Never let a Java-side exception escape to the WebView
                // (that kills the JS call with a useless generic message).
                // Return the exception's class/message/location instead so
                // the page's error toast names the real culprit.
                StackTraceElement[] st = t.getCause() != null
                        ? t.getCause().getStackTrace() : t.getStackTrace();
                String where = st != null && st.length > 0
                        ? String.valueOf(st[0]) : "";
                String cause = t.getCause() != null
                        ? t.getCause().getClass().getName() + ": "
                          + t.getCause().getMessage()
                        : t.getClass().getName() + ": " + t.getMessage();
                return "{\"error\": \"java "
                        + jsonSafe(cause + (where.isEmpty() ? "" : " @ " + where))
                        + "\"}";
            }
        }

        private String jsonSafe(String s) {
            return s == null ? "" : s.replace("\\", "\\\\")
                    .replace("\"", "'").replace("\n", " ").replace("\r", " ");
        }

        @JavascriptInterface
        public void openExternal(final String url) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    try {
                        Intent i = new Intent(Intent.ACTION_VIEW);
                        i.setDataAndType(Uri.parse(url), "video/*");
                        startActivity(i);
                    } catch (ActivityNotFoundException e) {
                        Toast.makeText(MainActivity.this,
                                "No external player installed",
                                Toast.LENGTH_SHORT).show();
                    }
                }
            });
        }
    }

    @Override
    protected void onPause() {
        if (webView != null) {
            webView.onPause();
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.onResume();
        }
    }

    @Override
    protected void onDestroy() {
        pyExecutor.shutdownNow();
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    /**
     * Hardware back: ask the page first (its androidBack() hook pops the
     * in-app screen stack); only when the page reports it has nothing left
     * do we exit, so the user is never trapped on the player screen.
     */
    @Override
    public void onBackPressed() {
        if (webView == null) {
            super.onBackPressed();
            return;
        }
        webView.evaluateJavascript(
                "(typeof window.androidBack === 'function') ? !!window.androidBack() : false",
                value -> {
                    if (!"true".equals(value)) {
                        finish();
                    }
                });
    }
}
