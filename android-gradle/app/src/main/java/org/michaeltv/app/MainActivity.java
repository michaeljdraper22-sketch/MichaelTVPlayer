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
        public String rpc(String cmd, String argsJson) {
            return bridge.call("rpc", cmd, argsJson).toString();
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
