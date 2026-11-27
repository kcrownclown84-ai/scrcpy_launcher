// 폰에서 app_process 로 실행되는 작은 도우미: 패키지명들을 받아 앱 아이콘을 PNG(base64)로 출력.
// 사용: CLASSPATH=/data/local/tmp/pl_icons.jar app_process / IconDump <size> <pkg> [pkg...]
// 출력: 한 줄에 "<pkg> <base64 png>"  (실패하면 "<pkg> -")
import android.content.Context;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
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
        return (Context) m.invoke(thread);
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
            try {
                Drawable d = pm.getApplicationIcon(pkg);
                Bitmap bmp;
                if (d instanceof BitmapDrawable && ((BitmapDrawable) d).getBitmap() != null) {
                    bmp = Bitmap.createScaledBitmap(((BitmapDrawable) d).getBitmap(), size, size, true);
                } else {
                    bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
                    Canvas c = new Canvas(bmp);
                    d.setBounds(0, 0, size, size);
                    d.draw(c);
                }
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                bmp.compress(Bitmap.CompressFormat.PNG, 100, bos);
                out.println(pkg + " " + Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP));
            } catch (Throwable t) {
                Throwable root = t;
                while (root.getCause() != null) root = root.getCause();
                out.println(pkg + " - " + root.getClass().getName() + ": " + root.getMessage());
                if (System.getenv("ICONDUMP_DEBUG") != null) t.printStackTrace(out);
            }
        }
        out.flush();
    }
}
