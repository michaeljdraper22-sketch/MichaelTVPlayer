package org.michaeltv.app;

import android.content.Context;

import com.google.android.gms.cast.CastMediaControlIntent;
import com.google.android.gms.cast.framework.CastOptions;
import com.google.android.gms.cast.framework.OptionsProvider;
import com.google.android.gms.cast.framework.SessionProvider;

import java.util.List;

/**
 * Cast SDK configuration.
 *
 * The SDK does NOT initialize itself: without this class (and the
 * OPTIONS_PROVIDER_CLASS_NAME manifest metadata that names it),
 * CastContext.getSharedInstance() throws, PlayerActivity silently catches
 * it and the Cast button never appears.  That was the state of the
 * v2.1.11/2.1.12 APKs.
 *
 * The Default Media Receiver (CC1AD845) is a receiver Google hosts for
 * every sender app — no Cast developer registration or custom receiver
 * page is needed.  It plays the HLS playlist the phone relays, including
 * the original H.264/AAC segments, so casting does not transcode.
 */
public final class CastOptionsProvider implements OptionsProvider {

    @Override
    public CastOptions getCastOptions(Context context) {
        return new CastOptions.Builder()
                .setReceiverApplicationId(
                        CastMediaControlIntent.DEFAULT_MEDIA_RECEIVER_APPLICATION_ID)
                .build();
    }

    @Override
    public List<SessionProvider> getAdditionalSessionProviders(Context context) {
        return null;
    }
}
