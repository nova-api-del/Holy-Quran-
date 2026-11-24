# Holy Quran Auto Publisher

مصنع تلقائي لمقاطع القرآن العمودية 1080x1920، مناسب لـ YouTube Shorts وInstagram Reels.

## الحقوق
لا يقوم المشروع بتحميل فيديوهات يوتيوب عشوائية أو إعادة استخدام تسجيلات محمية تلقائياً. يجب تفعيل مصادر صوتية تملك حقوق إعادة نشرها أو لديك إذن واضح باستخدامها.

المشاهد الحالية تأتي من Pexels. يمكن لاحقاً إضافة مصدر بصري مرخص آخر.

## ماذا يفعل؟
1. يختار قارئاً من المصادر المفعلة.
2. يحمل التسجيل المصرح باستخدامه.
3. يختار مشهداً عمودياً سينمائياً.
4. يصنع فيديو 1080x1920.
5. يضيف عنوان القرآن واسم القارئ.
6. يرفع الفيديو إلى YouTube Shorts.
7. يرفع الفيديو إلى Instagram Reels.
8. يحتفظ بسجل حتى لا يكرر المصدر نفسه.

## الأسرار المطلوبة في GitHub Actions
YOUTUBE_CLIENT_ID
YOUTUBE_CLIENT_SECRET
YOUTUBE_REFRESH_TOKEN
META_ACCESS_TOKEN
IG_USER_ID
CLOUDINARY_CLOUD_NAME
CLOUDINARY_API_KEY
CLOUDINARY_API_SECRET
PEXELS_API_KEY

## التشغيل
عدّل config/sources.json وأضف روابط مباشرة لتسجيلات مصرح بإعادة نشرها، ثم فعّل المصدر. بعد إعداد Secrets شغّل workflow يدوياً أولاً للاختبار، وبعدها سيعمل الجدول كل ساعتين.
