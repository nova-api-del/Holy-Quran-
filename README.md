# Holy Quran Auto Publisher

مصنع تلقائي لمقاطع القرآن العمودية 1080x1920 لـ YouTube Shorts وInstagram Reels.

## المصادر
تقدر تضيف روابط مباشرة أو روابط YouTube للتلاوات والمشاهد من خلال `config/sources.json`.
القائمة تتضمن أسماء مثل ياسر الدوسري وعبدالرحمن مسعد، ويمكن إضافة أي قارئ آخر.

## Cookies
إذا كانت بعض روابط YouTube تحتاج جلسة تسجيل دخول، ضع محتوى ملف cookies بصيغة Netscape في GitHub Secret باسم `YOUTUBE_COOKIES`.
**لا تضع الكوكيز داخل الكود أو داخل المستودع.**

الكوكيز هنا تُستخدم فقط بواسطة yt-dlp للوصول إلى المحتوى الذي تستطيع جلسة الحساب الوصول إليه. لا ترسلها لأي خدمة أخرى.

## Pipeline
1. يختار مصدراً.
2. يحمله عبر yt-dlp.
3. يختار مشهداً من YouTube أو Pexels.
4. يصنع فيديو 1080x1920 مدته حتى 58 ثانية.
5. يضيف هوية القناة واسم القارئ.
6. يرفع إلى YouTube Shorts وInstagram Reels.
7. يسجل المصدر حتى لا يتكرر.

## GitHub Secrets
YOUTUBE_COOKIES
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
ضع رابط YouTube للتلاوة في `source_url`، فعّل `enabled`، ثم شغّل Workflow يدوياً للاختبار. بعد نجاح الاختبار سيعمل الجدول كل ساعتين.
