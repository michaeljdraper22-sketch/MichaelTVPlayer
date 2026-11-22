package org.michaeltv.app;

import com.chaquo.python.PyObject;

/**
 * Process-wide handle to the Python bridge.
 *
 * MainActivity creates the Bridge; PlayerActivity needs it for two
 * lifecycle actions that must not depend on whichever Activity happens to
 * be alive:
 *
 *   * xt_live_stop on player teardown — releases the provider's single
 *     live connection and deletes the DVR cache;
 *   * nothing else today, but the foreground relay service also keeps the
 *     process (and this bridge) alive while casting with the screen off.
 *
 * The relay itself is a module-level singleton inside the Python bridge,
 * so a recreated MainActivity/Bridge still stops the same relay.
 */
public final class AppBridge {

    private static volatile PyObject bridge;

    private AppBridge() {
    }

    static void set(PyObject value) {
        bridge = value;
    }

    static String rpc(String cmd, String argsJson) {
        PyObject value = bridge;
        if (value == null) {
            return "{\"error\":\"bridge not ready\"}";
        }
        try {
            return value.callAttr("rpc", cmd, argsJson).toString();
        } catch (Throwable t) {
            return "{\"error\":\"" + t.getClass().getSimpleName() + "\"}";
        }
    }
}
