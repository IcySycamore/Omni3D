package com.omni3d.capture;

import android.app.PendingIntent;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.graphics.Bitmap;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraManager;
import android.net.http.SslError;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.SslErrorHandler;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * 安卓原生 AR 采集辅助类：
 * 1) 传感器姿态（旋转向量 -> 欧拉角）
 * 2) 后置相机内参（CameraCharacteristics LENS_INTRINSIC_CALIBRATION）
 * 3) （保留）自集成安装 AREngine Server / NDK 桥，备用
 */
public final class ARHelper {
    private static final String TAG = "ARHelper";

    // ---- 姿态（旋转向量 -> 欧拉角）----
    private static SensorManager sSensorManager;
    private static SensorEventListener sListener;
    private static volatile float sYaw, sPitch, sRoll; // 度

    public static void startSensors(Context ctx) {
        if (sSensorManager != null) return;
        sSensorManager = (SensorManager) ctx.getSystemService(Context.SENSOR_SERVICE);
        if (sSensorManager == null) return;
        Sensor g = sSensorManager.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR);
        if (g == null) g = sSensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR);
        if (g == null) return;
        sListener = new SensorEventListener() {
            @Override public void onSensorChanged(SensorEvent e) {
                if (e.values.length < 4) return;
                float[] r = new float[9];
                SensorManager.getRotationMatrixFromVector(r, e.values);
                float[] o = new float[3];
                SensorManager.getOrientation(r, o);
                sYaw   = (float) Math.toDegrees(o[0]); // -180..180
                sPitch = (float) Math.toDegrees(o[1]);
                sRoll  = (float) Math.toDegrees(o[2]);
            }
            @Override public void onAccuracyChanged(Sensor s, int a) {}
        };
        sSensorManager.registerListener(sListener, g, SensorManager.SENSOR_DELAY_GAME);
        Log.i(TAG, "sensors started");
    }

    public static void stopSensors(Context ctx) {
        if (sSensorManager != null && sListener != null) {
            sSensorManager.unregisterListener(sListener);
            sSensorManager = null; sListener = null;
        }
    }

    public static float getYaw() { return sYaw; }
    public static float getPitch() { return sPitch; }
    public static float getRoll() { return sRoll; }

    // ---- 混合内容放行 + SSL 证书忽略（frp HTTPS 隧道场景）----
    // 网页从 https://域名 加载后，页面 fetch http://127.0.0.1:50687（App 本地 AR 桥）
    // 属于混合内容，Android WebView 默认阻止 → 必须显式放行。
    // 另外 SakuraFrp 分配的 https 证书是临时的，WebView 默认拒绝 → 需忽略 SSL 错误。
    // 通过遍历窗口 View 树找到 Qt 创建的 WebView 并设置（Qt 无此 API）。
    // ⚠️ WebView 方法必须在 Android 主线程（UI Looper）调用；本方法可能被
    //    Qt 主循环线程（qtMainLoopThread）调用 → 用 runOnUiThread 切换。
    public static void enableMixedContent(Context ctx) {
        try {
            if (!(ctx instanceof Activity)) return;
            final Activity act = (Activity) ctx;
            act.runOnUiThread(new Runnable() {
                @Override public void run() {
                    View root = act.getWindow().getDecorView();
                    if (root == null) return;
                    enableMixedContentIn(root);
                }
            });
        } catch (Throwable t) {
            Log.w(TAG, "enableMixedContent: " + t);
        }
    }

    private static void enableMixedContentIn(View v) {
        if (v == null) return;
        try {
            if (v instanceof WebView) {
                WebView wv = (WebView) v;
                // 1) 混合内容放行（https 页面 fetch 本地 http 桥）
                wv.getSettings().setMixedContentMode(
                        WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
                // 2) 忽略 SSL 证书错误（SakuraFrp 临时证书）——只处理一次
                // ⚠️ 不能用 getWebViewClient() instanceof 判断：华为 WebView 可能
                //    返回 null → 每秒都重新包装+reload → 无限 reload 循环（黑屏）
                if (sHandledWebViews.add(wv)) {
                    wv.setWebViewClient(new AllowSslWebViewClient(wv.getWebViewClient()));
                    String u = wv.getUrl();
                    if (u != null && !u.isEmpty()) {
                        wv.reload();
                        Log.i(TAG, "webview: ssl client installed, reload " + u);
                    }
                }
                Log.i(TAG, "webview: mixed content ALLOWED + ssl errors ignored");
            }
        } catch (Throwable ignored) {
        }
        if (v instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) {
                enableMixedContentIn(g.getChildAt(i));
            }
        }
    }

    /** 已处理过的 WebView（弱引用，避免泄漏；防止每秒重复包装+reload） */
    private static final java.util.Set<WebView> sHandledWebViews =
            java.util.Collections.newSetFromMap(new java.util.WeakHashMap<WebView, Boolean>());

    /** 包装 Qt 的 WebViewClient：忽略 SSL 错误，其余回调转发原 client。 */
    private static final class AllowSslWebViewClient extends WebViewClient {
        private final WebViewClient base;

        AllowSslWebViewClient(WebViewClient base) {
            this.base = base;
            Log.i(TAG, "AllowSslWebViewClient created, base=" + (base == null ? "null" : base.getClass().getName()));
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            Log.i(TAG, "onReceivedSslError -> proceed: " + error);
            handler.proceed();
        }

        @Override
        public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
            Log.i(TAG, "onReceivedError: code=" + errorCode + " desc=" + description + " url=" + failingUrl);
            if (base != null) base.onReceivedError(view, errorCode, description, failingUrl);
            else super.onReceivedError(view, errorCode, description, failingUrl);
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            Log.i(TAG, "shouldOverrideUrlLoading: " + url);
            if (base != null) return base.shouldOverrideUrlLoading(view, url);
            return super.shouldOverrideUrlLoading(view, url);
        }

        @Override
        public void onLoadResource(WebView view, String url) {
            if (base != null) base.onLoadResource(view, url);
            else super.onLoadResource(view, url);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            Log.i(TAG, "onPageFinished: " + url);
            if (base != null) base.onPageFinished(view, url);
            else super.onPageFinished(view, url);
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            Log.i(TAG, "onPageStarted: " + url);
            if (base != null) base.onPageStarted(view, url, favicon);
            else super.onPageStarted(view, url, favicon);
        }
    }

    // ---- 后置相机内参 [fx, fy, cx, cy] ----
    public static float[] getIntrinsics(Context ctx) {
        try {
            CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
            if (cm == null) return null;
            String[] ids = cm.getCameraIdList();
            for (String id : ids) {
                CameraCharacteristics cc = cm.getCameraCharacteristics(id);
                Integer facing = cc.get(CameraCharacteristics.LENS_FACING);
                if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) {
                    float[] intr = cc.get(CameraCharacteristics.LENS_INTRINSIC_CALIBRATION);
                    if (intr != null && intr.length >= 4) return intr;
                }
            }
        } catch (Exception ex) {
            Log.e(TAG, "getIntrinsics failed", ex);
        }
        return null;
    }

    /**
     * 通过 PackageInstaller 安装 Server APK（纯 framework，无需 androidx）。
     * @param apkPath assets 释放后的完整路径
     * @return 0=成功触发安装，-1=失败
     */
    public static int installServerApk(Context context, String apkPath) {
        File apk = new File(apkPath);
        if (!apk.exists()) {
            Log.e(TAG, "apk not exists: " + apkPath);
            return -1;
        }
        try {
            PackageInstaller pi = context.getPackageManager().getPackageInstaller();
            PackageInstaller.SessionParams params =
                    new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
            params.setAppPackageName("com.huawei.arengine.service");
            int sessionId = pi.createSession(params);
            PackageInstaller.Session session = pi.openSession(sessionId);
            try (OutputStream out = session.openWrite("base.apk", 0, apk.length())) {
                try (InputStream in = new FileInputStream(apk)) {
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        out.write(buf, 0, n);
                    }
                }
                session.fsync(out);
            }
            // 安装确认回调 -> 本 Activity（QtActivity）
            Intent intent = new Intent(context, context.getClass());
            int flags = Intent.FLAG_ACTIVITY_NEW_TASK;
            PendingIntent pi2 = PendingIntent.getActivity(context, 0, intent, flags);
            session.commit(pi2.getIntentSender());
            session.close();
            Log.i(TAG, "install session committed: " + sessionId);
            return 0;
        } catch (Exception e) {
            Log.e(TAG, "install failed", e);
            return -1;
        }
    }
}
