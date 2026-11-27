// 폰에서 app_process 로 실행되는 작은 도우미: 패키지명들을 받아 앱 아이콘을 PNG(base64)로 출력.
// 사용: CLASSPATH=/data/local/tmp/pl_icons.jar app_process / IconDump <size> <pkg> [pkg...]
// 출력: 한 줄에 "<pkg> <base64 png>"  (실패하면 "<pkg> -")
import android.app.Application;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.PackageManager;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.res.Resources;
import android.graphics.Bitmap;
import android.graphics.BitmapShader;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.drawable.AdaptiveIconDrawable;
import android.os.Build;
import android.graphics.Canvas;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.os.Looper;
import android.util.Base64;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;

public final class IconDump {

    private static Context systemContext() throws Exception {
        Class<?> at = Class.forName("android.app.ActivityThread");
        Constructor<?> ctor = at.getDeclaredConstructor();
        ctor.setAccessible(true);
        Object thread = ctor.newInstance();
        Field cur = at.getDeclaredField("sCurrentActivityThread");
        cur.setAccessible(true);
        cur.set(null, thread);
        Method m = at.getDeclaredMethod("getSystemContext");
        m.setAccessible(true);
        Context ctx = (Context) m.invoke(thread);
        // AdaptiveIconDrawable 등은 ActivityThread.currentApplication() 이 필요 → 가짜 Application 연결
        try {
            Application app = Application.class.newInstance();
            Method attach = ContextWrapper.class.getDeclaredMethod("attachBaseContext", Context.class);
            attach.setAccessible(true);
            attach.invoke(app, ctx);
            Field init = at.getDeclaredField("mInitialApplication");
            init.setAccessible(true);
            init.set(thread, app);
        } catch (Throwable t) {
            if (System.getenv("ICONDUMP_DEBUG") != null) t.printStackTrace();
        }
        return ctx;
    }

    private static Drawable loadIcon(PackageManager pm, String pkg, int way) throws Exception {
        ApplicationInfo ai = pm.getApplicationInfo(pkg, 0);
        switch (way) {
            case 0:
                return pm.getApplicationIcon(ai);
            case 1:
                return ai.loadUnbadgedIcon(pm);
            case 2: {
                Resources r = pm.getResourcesForApplication(ai);
                return r.getDrawableForDensity(ai.icon != 0 ? ai.icon : android.R.drawable.sym_def_app_icon, 480, null);
            }
            default: {
                Intent it = pm.getLaunchIntentForPackage(pkg);
                if (it == null) return null;
                return pm.getActivityIcon(it);
            }
        }
    }

    private static Bitmap render(Drawable d, int size) {
        if (d instanceof BitmapDrawable && ((BitmapDrawable) d).getBitmap() != null) {
            return Bitmap.createScaledBitmap(((BitmapDrawable) d).getBitmap(), size, size, true);
        }
        Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(bmp);
        try {
            d.setBounds(0, 0, size, size);
            d.draw(c);
        } catch (Throwable t) {
            // 적응형 아이콘을 직접 합성 (배경 + 전경, 둥근 사각형으로 자름)
            if (Build.VERSION.SDK_INT < 26 || !(d instanceof AdaptiveIconDrawable)) throw t;
            AdaptiveIconDrawable a = (AdaptiveIconDrawable) d;
            bmp.eraseColor(0);
            Bitmap layer = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
            Canvas lc = new Canvas(layer);
            int inset = -size / 4;  // 적응형 아이콘은 가장자리 1/6 정도가 잘려 보이는 구조
            for (Drawable part : new Drawable[]{a.getBackground(), a.getForeground()}) {
                if (part == null) continue;
                part.setBounds(inset, inset, size - inset, size - inset);
                part.draw(lc);
            }
            Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
            p.setShader(new BitmapShader(layer, Shader.TileMode.CLAMP, Shader.TileMode.CLAMP));
            c.drawRoundRect(new RectF(0, 0, size, size), size * 0.25f, size * 0.25f, p);
        }
        return bmp;
    }

    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(System.out, true, "UTF-8");
        try {
            Looper.prepareMainLooper();
        } catch (Throwable ignored) {
        }
        int size = Integer.parseInt(args[0]);
        PackageManager pm = systemContext().getPackageManager();
        for (int i = 1; i < args.length; i++) {
            String pkg = args[i];
            StringBuilder errs = new StringBuilder();
            Bitmap bmp = null;
            // 기기마다(특히 삼성) 되는 방법이 달라서 여러 방법을 차례로 시도
            for (int way = 0; way < 4 && bmp == null; way++) {
                try {
                    Drawable d = loadIcon(pm, pkg, way);
                    if (d != null) bmp = render(d, size);
                } catch (Throwable t) {
                    Throwable root = t;
                    while (root.getCause() != null) root = root.getCause();
                    errs.append(" [").append(way).append("] ").append(root.getClass().getSimpleName())
                        .append(": ").append(root.getMessage());
                    if (System.getenv("ICONDUMP_DEBUG") != null) t.printStackTrace(out);
                }
            }
            if (bmp != null) {
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                bmp.compress(Bitmap.CompressFormat.PNG, 100, bos);
                out.println(pkg + " " + Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP));
            } else {
                out.println(pkg + " -" + errs);
            }
        }
        out.flush();
    }
}
