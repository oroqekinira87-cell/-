import sqlite3
import random
import telebot
from telebot import types
import os
import threading
import time
import requests
from flask import Flask, jsonify
from datetime import datetime, date
from io import BytesIO
import re

# ==========================================
# قاموس الإيموجيات المميزة (Premium Custom Emojis)
# ==========================================
E = {
    'fire': '5424972470023104089', 'check': '5206607081334906820', 'sparkles': '5325547803936572038',
    'gem': '5427168083074628963', 'pencil': '5395444784611480792', 'settings': '5341715473882955310',
    'crown': '5217822164362739968', 'chart': '5231200819986047254', 'warning': '5447644880824181073',
    'trophy': '5188344996356448758', 'people': '5258513401784573443', 'link': '5271604874419647061',
    'picture': '5375074927252621134', 'arrow': '5416117059207572332', 'cross': '5210952531676504517',
    'bulb': '5422439311196834318', 'bell': '5458603043203327669', 'python': '5260480440971570446',
}

# ==========================================
# الإعدادات (تدعم متغيرات بيئة Render.com)
# ==========================================
API_TOKEN = os.environ.get('API_TOKEN', '8591586628:AAGo85RBCysZQ6Bvp1y3lLeI-85iuQRhJ0w')
SUPER_ADMIN = int(os.environ.get('SUPER_ADMIN', '8439198448'))
PORT = int(os.environ.get('PORT', 5000))
APP_URL = os.environ.get('APP_URL', '').rstrip('/')

bot = telebot.TeleBot(API_TOKEN, parse_mode='MarkdownV2')
user_temp_photos = {}

# ==========================================
# خادم Flask والمنبه (Keep-Alive)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "bot": "Photo Rating Bot"})

@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

def keep_alive_ping():
    while True:
        try:
            if APP_URL:
                requests.get(f'{APP_URL}/health', timeout=15)
        except:
            pass
        time.sleep(300)

# ==========================================
# قاعدة البيانات (آمنة للـ Threading)
# ==========================================
conn = sqlite3.connect('bot_data.db', check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

cursor.executescript('''
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, photos_rated INTEGER DEFAULT 0,
    last_photo_time INTEGER DEFAULT 0, nsfw_warnings INTEGER DEFAULT 0, banned INTEGER DEFAULT 0,
    daily_count INTEGER DEFAULT 0, last_submit_date TEXT
);
CREATE TABLE IF NOT EXISTS nsfw_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, photo_id TEXT, detected_at TEXT, method TEXT
);
''')

# تحديث الجداول تلقائياً لمنع الأخطاء
columns_to_add = [
    ("photos_rated", "INTEGER DEFAULT 0"), ("last_photo_time", "INTEGER DEFAULT 0"),
    ("nsfw_warnings", "INTEGER DEFAULT 0"), ("banned", "INTEGER DEFAULT 0"),
    ("daily_count", "INTEGER DEFAULT 0"), ("last_submit_date", "TEXT")
]
for col_name, col_type in columns_to_add:
    try: cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};")
    except sqlite3.OperationalError: pass

conn.commit()

# ==========================================
# الدوال المساعدة
# ==========================================
def get_setting(key, default=''):
    with db_lock:
        cursor.execute('SELECT value FROM settings WHERE key=?', (key,))
        row = cursor.fetchone()
        return row[0] if row else default

def set_setting(key, value):
    with db_lock:
        cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()

def escape_md(text):
    if not text: return ""
    special_chars = '_*[]()~`>#+-=|{}.!'
    for char in special_chars: text = text.replace(char, f'\\{char}')
    return text

def is_admin(user_id):
    with db_lock:
        cursor.execute('SELECT user_id FROM admins WHERE user_id=?', (user_id,))
        return cursor.fetchone() is not None

def is_banned(user_id):
    with db_lock:
        cursor.execute('SELECT banned FROM users WHERE user_id=?', (user_id,))
        row = cursor.fetchone()
        return row[0] == 1 if row else False

def check_sub(user_id):
    sub_chan = get_setting('sub_channel', '')
    if not sub_chan: return True
    try:
        member = bot.get_chat_member(sub_chan, user_id)
        return member.status in ['creator', 'administrator', 'member']
    except: return True

def btn(text, callback_data=None, url=None, emoji_id=None, style="primary"):
    button_kwargs = {'text': text}
    if url: button_kwargs['url'] = url
    if callback_data: button_kwargs['callback_data'] = callback_data
    if emoji_id: button_kwargs['icon_custom_emoji_id'] = emoji_id
    if style: button_kwargs['style'] = style
    return types.InlineKeyboardButton(**button_kwargs)

# ==========================================
# نظام الذكاء الاصطناعي (بدون مفتاح API - مجاني للكل)
# ==========================================
def analyze_and_rate_photo(photo_file_id):
    """
    1. يفحص الإباحية عبر HuggingFace.
    2. يولد وصف للصورة.
    3. يرسل الوصف لـ Pollinations AI لكتابة تقرير تقييم صارم من 0 إلى 10.
    """
    try:
        file_info = bot.get_file(photo_file_id)
        downloaded = bot.download_file(file_info.file_path)
    except: return True, 'download_error', 0, "خطأ في تحميل الصورة"

    # 1. فحص الإباحية
    try:
        api_url = "https://api-inference.huggingface.co/models/Falconsai/nsfw_image_detection"
        response = requests.post(api_url, data=downloaded, timeout=20)
        if response.status_code == 200:
            result = response.json()
            for item in result:
                if item.get('label', '').lower() == 'nsfw' and item.get('score', 0) > 0.15:
                    return True, 'AI_Nsfw_Classifier', 0, "محتوى غير لائق"
    except: pass

    # 2. توليد وصف للصورة (نموذج BLIP)
    caption = "a person"
    try:
        api_url_blip = "https://api-inference.huggingface.co/models/Salesforce/blip-image-captioning-large"
        response = requests.post(api_url_blip, data=downloaded, timeout=20)
        if response.status_code == 200:
            caption = response.json()[0].get('generated_text', 'a person')
    except: pass

    # فحص احتياطي للون البشرة
    try:
        from PIL import Image
        img = Image.open(BytesIO(downloaded)).convert('RGB')
        img.thumbnail((200, 200))
        skin_pixels = 0; total_pixels = 0
        for r, g, b in list(img.getdata()):
            total_pixels += 1
            if r > 95 and g > 40 and b > 20 and max(r, g, b) - min(r, g, b) > 15 and abs(r - g) > 15 and r > g and r > b:
                skin_pixels += 1
        if (skin_pixels / total_pixels if total_pixels > 0 else 0) > 0.45:
            return True, 'Skin_Tone_Fallback', 0, "محتوى غير لائق"
    except: pass

    # 3. كتابة تقرير التقييم الصارم (Pollinations Text AI)
    try:
        prompt = (
            f"أنت ناقد صور محترف وصارم جداً ولا تجامل أحداً. بناءً على وصف الصورة التالي: '{caption}'. "
            f"اكتب تقرير تقييم مختصر باللغة العربية يشمل بالترتيب: تقييم الإضاءة، تقييم الجودة، تقييم زاوية التصوير، "
            f"تقييم الملابس، تقييم الوقفة، وتقييم الخلفية. لا تكن لطيفاً، انقد بصدق. "
            f"في السطر الأخير اكتب حصرياً: التقييم النهائي: X/10 (حيث X رقم حقيقي من 0 إلى 10 بناءً على الجودة)."
        )
        encoded_prompt = requests.utils.quote(prompt)
        text_res = requests.get(f"https://text.pollinations.ai/{encoded_prompt}", timeout=30)
        
        if text_res.status_code == 200:
            report_text = text_res.text.strip()
            # استخراج التقييم النهائي
            match = re.search(r'(\d+)\s*/\s*10', report_text)
            rating = int(match.group(1)) if match else random.randint(4, 8)
            rating = max(0, min(10, rating))
            
            return False, 'Safe', rating, report_text
    except: pass

    # احتياطي نهائي إذا فشل النص
    return False, 'Safe', random.randint(5, 8), "صورة مقبولة، الإضاءة والزاوية بحاجة لتحسين بسيط."

# ==========================================
# لوحة تحكم الأدمن الشاملة
# ==========================================
def send_admin_panel(chat_id, message_id=None):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        btn("📤 رفع صورة", callback_data='admin_upload_photo', emoji_id=E['picture'], style="success"),
        btn("قناة التقييم", callback_data='menu_channel', emoji_id=E['link'], style="success")
    )
    markup.add(
        btn("الاشتراك الإجباري", callback_data='menu_sub', emoji_id=E['bell'], style="primary"),
        btn("الأدمنية", callback_data='menu_admins', emoji_id=E['people'], style="primary")
    )
    markup.add(
        btn("الحماية والمحظورين", callback_data='menu_protection', emoji_id=E['warning'], style="danger"),
        btn("الإحصائيات", callback_data='detailed_stats', emoji_id=E['chart'], style="primary")
    )
    markup.add(
        btn("الإعدادات العامة", callback_data='menu_settings', emoji_id=E['settings'], style="success"),
        btn("إرسال جماعي", callback_data='menu_broadcast', emoji_id=E['fire'], style="danger")
    )
    markup.add(
        btn("صورة ثابتة", callback_data='set_fixed_img', emoji_id=E['picture'], style="success"),
        btn("تغيير المطور", callback_data='change_dev', emoji_id=E['crown'], style="primary")
    )
    markup.add(
        btn("تغيير السورس", callback_data='change_source', emoji_id=E['python'], style="primary")
    )
    text = '> أهلاً بك في *لوحة تحكم الأدمن الشاملة*\\.\n> اختر القسم المطلوب للتحكم الكامل بالبوت\\.'
    if message_id:
        try: bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
        except: bot.send_message(chat_id, text, reply_markup=markup)
    else: bot.send_message(chat_id, text, reply_markup=markup)

@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    with db_lock:
        cursor.execute('INSERT OR IGNORE INTO users (user_id, photos_rated, last_photo_time, nsfw_warnings, banned, daily_count, last_submit_date) VALUES (?, 0, 0, 0, 0, 0, ?)', (user_id, date.today().isoformat()))
        conn.commit()
    
    if is_banned(user_id):
        bot.send_message(message.chat.id, '> 🚫 تم حظر حسابك من استخدام البوت بسبب إرسال محتوى غير لائق\\.')
        return
        
    if not is_admin(user_id) and get_setting('maintenance_mode', '0') == '1':
        bot.send_message(message.chat.id, '> 🔧 البوت حالياً تحت الصيانة\\.\n> سنرجع قريباً بإذن الله\\.')
        return
        
    if not check_sub(user_id):
        sub_chan = get_setting('sub_channel', '').replace('@', '')
        markup = types.InlineKeyboardMarkup()
        markup.add(btn("🔔 اشترك بالقناة", url=f'https://t.me/{sub_chan}', style="primary"))
        bot.send_message(message.chat.id, '> اشترك بالقناة أولاً حتى تقدر تسيطر وتستخدم البوت براحتك\\.', reply_markup=markup)
        return
        
    if is_admin(user_id): send_admin_panel(message.chat.id)
    else:
        markup = types.InlineKeyboardMarkup(row_width=2)
        dev_user = get_setting('dev_user', 'Telegram').replace('@', '')
        source_chan = get_setting('source_channel', 'Telegram').replace('@', '')
        target_chan = get_setting('target_channel', 'Telegram').replace('@', '')
        dev_url = f'https://t.me/{dev_user}' if not dev_user.startswith('http') else dev_user
        source_url = f'https://t.me/{source_chan}' if not source_chan.startswith('http') else source_chan
        target_url = f'https://t.me/{target_chan}' if not target_chan.startswith('http') else target_chan
        markup.add(btn("📸 دز صورة للتقييم", callback_data='user_send_pic', emoji_id=E['picture'], style="success"))
        markup.add(btn("👤 حسابي", callback_data='my_stats', emoji_id=E['people'], style="primary"))
        markup.add(btn("📷 قناة صوركم", url=target_url, emoji_id=E['link'], style="danger"))
        markup.add(btn("👨‍💻 المطور", url=dev_url, emoji_id=E['crown'], style="primary"), btn("📡 قناة السورس", url=source_url, emoji_id=E['python'], style="primary"))
        caption = '> هلا بيك ببوت تقييم الصور\\! دزلنا صورتك ونقيمها إلك وننشرها بالقناة الأحسن\\.'
        fixed_img = get_setting('fixed_image_id', '')
        if fixed_img: bot.send_photo(message.chat.id, fixed_img, caption=caption, reply_markup=markup)
        else: bot.send_message(message.chat.id, caption, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    chat_id = call.message.chat.id; user_id = call.from_user.id
    
    if call.data == 'user_send_pic':
        bot.send_message(chat_id, '> دز الصورة مالتك حالياً حتى نبلش ونقيمها إلك\\.')
    elif call.data == 'admin_upload_photo':
        bot.send_message(chat_id, '> 📤 أرسل الصورة الآن للتقييم والنشر\\.\n> (الأدمن معفى من الحد اليومي والانتظار)')
    elif call.data == 'my_stats':
        with db_lock:
            cursor.execute('SELECT photos_rated, daily_count FROM users WHERE user_id=?', (user_id,))
            row = cursor.fetchone()
            count = row[0] if row else 0
            daily = row[1] if row else 0
        bot.answer_callback_query(call.id, f'إجمالي صورك: {count}\nصور اليوم: {daily}/3', show_alert=True)
    elif call.data == 'rate_click':
        bot.answer_callback_query(call.id, 'شكراً لتقييمك للصورة! ⭐', show_alert=False)
    elif call.data in ['gender_boy', 'gender_girl']:
        if user_id not in user_temp_photos:
            bot.send_message(chat_id, '> انتهت جلسة الصورة، يرجى إرسال الصورة من جديد\\.')
            return
        target_chan = get_setting('target_channel', '')
        if not target_chan:
            bot.send_message(chat_id, '> ما مضافة قناة للنشر لحد الان، راسل الأدمن حتى يضيفها\\.')
            return
            
        data = user_temp_photos.pop(user_id)
        photo_id = data['photo_id']
        
        bot.send_chat_action(chat_id, 'typing')
        bot.edit_message_text('> 🤖 الذكاء الاصطناعي يحلل الصورة بدقة وينشئ التقرير\\.\\.\\.', chat_id, call.message.message_id)
        
        is_nsfw, method, rating, report_text = analyze_and_rate_photo(photo_id)
        
        if is_nsfw:
            with db_lock:
                cursor.execute('INSERT INTO nsfw_logs (user_id, photo_id, detected_at, method) VALUES (?, ?, ?, ?)', (user_id, photo_id, datetime.now().isoformat(), method))
                cursor.execute('SELECT nsfw_warnings FROM users WHERE user_id=?', (user_id,))
                row = cursor.fetchone(); warnings = row[0] + 1 if row else 1
                
                if warnings >= 3:
                    cursor.execute('UPDATE users SET banned=1, nsfw_warnings=? WHERE user_id=?', (warnings, user_id))
                    conn.commit()
                    bot.send_message(chat_id, '> ⚠️ تم رفض الصورة\\!\n> الصورة تحتوي على محتوى غير لائق\\.\n> لن يتم نشرها\\.')
                    bot.send_message(chat_id, '> 🚫 لقد تجاوزت الحد المسموح من التحذيرات\\.\n> تم حظر حسابك نهائياً من استخدام البوت\\.')
                else:
                    cursor.execute('UPDATE users SET nsfw_warnings=? WHERE user_id=?', (warnings, user_id))
                    conn.commit()
                    bot.send_message(chat_id, '> ⚠️ تم رفض الصورة\\!\n> الصورة تحتوي على محتوى غير لائق أو إيحاء\\.\n> لن يتم نشرها\\.')
                    bot.send_message(chat_id, f'> 🚨 لديك إنذار {warnings} من 3\\.\n> بقي لك {3 - warnings} تحذير وسيتم حظر حسابك نهائياً\\.')
            return

        # تجهيز النص للنشر
        caption_text = (
            f'> 📸 *تقرير تقييم الصورة*\n\n'
            f'> {escape_md(report_text)}\n\n'
            f'> ⭐ *التقييم الإجمالي:* {rating}/10\n'
        )
        
        channel_markup = types.InlineKeyboardMarkup(row_width=2)
        bot_username = bot.get_me().username
        channel_markup.add(
            btn("🤖 دخول البوت", url=f"https://t.me/{bot_username}?start=ref", style="success"),
            btn("⭐ تقييم صورة", callback_data='rate_click', style="primary")
        )
        
        try:
            bot.send_photo(target_chan, photo_id, caption=caption_text, reply_markup=channel_markup)
            bot.send_message(chat_id, '> ✅ عاشت إيدك، تم تحليل الصورة ونشرها بقناة التقييم بنجاح\\!')
            with db_lock:
                cursor.execute('UPDATE users SET photos_rated = photos_rated + 1 WHERE user_id=?', (user_id,))
                conn.commit()
        except Exception as e:
            bot.send_message(chat_id, f'> صار خطأ بالنشر، تأكد البوت أدمن بالقناة\\.\nالخطأ: {escape_md(str(e))}')

    # === أزرار الأدمن ===
    if is_admin(user_id):
        if call.data == 'back_admin': send_admin_panel(chat_id, call.message.message_id)
        elif call.data == 'detailed_stats':
            with db_lock:
                cursor.execute("SELECT COUNT(*) FROM users"); total_users = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM users WHERE banned=1"); banned_users = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM nsfw_logs"); nsfw_attempts = cursor.fetchone()[0]
                cursor.execute("SELECT SUM(photos_rated) FROM users"); total_ratings = cursor.fetchone()[0] or 0
            stats_text = (
                f'> 📊 *الإحصائيات التفصيلية*\n\n'
                f'> 👥 إجمالي المستخدمين: `{total_users}`\n'
                f'> 🚫 المحظورون: `{banned_users}`\n'
                f'> ⚠️ محاولات إباحية: `{nsfw_attempts}`\n'
                f'> 📸 إجمالي الصور المقيّمة: `{total_ratings}`\n'
            )
            bot.edit_message_text(stats_text, chat_id, call.message.message_id)
        elif call.data == 'menu_protection':
            markup = types.InlineKeyboardMarkup()
            markup.add(btn("عرض المحظورين", callback_data='view_banned', emoji_id=E['people'], style="primary"))
            markup.add(btn("إلغاء حظر مستخدم", callback_data='unban_user_input', emoji_id=E['check'], style="success"))
            markup.add(btn("رجوع", callback_data='back_admin', emoji_id=E['arrow'], style="primary"))
            bot.edit_message_text('> 🛡️ *قسم الحماية والمحظورين*', chat_id, call.message.message_id, reply_markup=markup)
        elif call.data == 'view_banned':
            with db_lock:
                cursor.execute("SELECT user_id, nsfw_warnings FROM users WHERE banned=1")
                banned_list = cursor.fetchall()
            if banned_list:
                text = '> 🚫 *قائمة المحظورين:*\n\n'
                for b in banned_list: text += f'> ID: `{b[0]}` \\| التحذيرات: {b[1]}\n'
            else: text = '> ✅ لا يوجد مستخدمون محظورون حالياً\\.'
            bot.send_message(chat_id, text)
        elif call.data == 'unban_user_input':
            msg = bot.send_message(chat_id, '> دز أيدي \\(ID\\) المستخدم لإلغاء حظره:')
            bot.register_next_step_handler(msg, process_unban)
        elif call.data == 'menu_settings':
            maint_status = "مفعّل 🔴" if get_setting('maintenance_mode', '0') == '1' else "معطّل 🟢"
            markup = types.InlineKeyboardMarkup()
            markup.add(btn(f"وضع الصيانة ({maint_status})", callback_data='toggle_maintenance', emoji_id=E['settings'], style="danger" if get_setting('maintenance_mode', '0') == '1' else "success"))
            markup.add(btn("رجوع", callback_data='back_admin', emoji_id=E['arrow'], style="primary"))
            bot.edit_message_text('> ⚙️ *الإعدادات العامة*', chat_id, call.message.message_id, reply_markup=markup)
        elif call.data == 'toggle_maintenance':
            current = get_setting('maintenance_mode', '0')
            set_setting('maintenance_mode', '1' if current == '0' else '0')
            send_admin_panel(chat_id, call.message.message_id)
        elif call.data == 'menu_broadcast':
            markup = types.InlineKeyboardMarkup()
            markup.add(btn("إرسال رسالة الآن", callback_data='do_broadcast', emoji_id=E['fire'], style="danger"))
            markup.add(btn("رجوع", callback_data='back_admin', emoji_id=E['arrow'], style="primary"))
            bot.edit_message_text('> 📣 إرسال جماعي لكل المستخدمين:', chat_id, call.message.message_id, reply_markup=markup)
        elif call.data == 'do_broadcast':
            msg = bot.send_message(chat_id, '> دز الرسالة النصية ال تريدها أن توصل للجميع:')
            bot.register_next_step_handler(msg, process_broadcast)
        elif call.data == 'change_dev':
            msg = bot.send_message(chat_id, '> دز معرف المطور الجديد \\(مثال: @username\\):')
            bot.register_next_step_handler(msg, save_dev)
        elif call.data == 'change_source':
            msg = bot.send_message(chat_id, '> دز رابط أو معرف قناة السورس الجديدة:')
            bot.register_next_step_handler(msg, save_source)
        elif call.data == 'menu_channel':
            markup = types.InlineKeyboardMarkup()
            markup.add(btn("إضافة قناة", callback_data='add_target_chan', emoji_id=E['check'], style="success"), btn("حذف القناة", callback_data='del_target_chan', emoji_id=E['cross'], style="danger"))
            markup.add(btn("عرض القناة", callback_data='show_target_chan', emoji_id=E['link'], style="primary"))
            markup.add(btn("رجوع", callback_data='back_admin', emoji_id=E['arrow'], style="primary"))
            bot.edit_message_text('> خيارات إعداد قناة التقييم والنشر:', chat_id, call.message.message_id, reply_markup=markup)
        elif call.data == 'add_target_chan':
            msg = bot.send_message(chat_id, '> دز معرف أو أيدي القناة \\(مثال: @channel\\):')
            bot.register_next_step_handler(msg, save_target_channel)
        elif call.data == 'show_target_chan':
            target = get_setting('target_channel', 'ما مضافة قناة لحد الان')
            bot.send_message(chat_id, f'> القناة الحالية للنشر: {escape_md(target)}')
        elif call.data == 'del_target_chan':
            set_setting('target_channel', ''); bot.send_message(chat_id, '> تم حذف قناة التقييم بنجاح\\.')
        elif call.data == 'menu_sub':
            markup = types.InlineKeyboardMarkup()
            markup.add(btn("إضافة قناة", callback_data='add_sub_chan', emoji_id=E['check'], style="success"), btn("حذف القناة", callback_data='del_sub_chan', emoji_id=E['cross'], style="danger"))
            markup.add(btn("رجوع", callback_data='back_admin', emoji_id=E['arrow'], style="primary"))
            bot.edit_message_text('> إعدادات قناة الاشتراك الإجباري:', chat_id, call.message.message_id, reply_markup=markup)
        elif call.data == 'add_sub_chan':
            msg = bot.send_message(chat_id, '> دز معرف قناة الاشتراك الإجباري \\(مثال: @channel\\):')
            bot.register_next_step_handler(msg, save_sub_channel)
        elif call.data == 'del_sub_chan':
            set_setting('sub_channel', ''); bot.send_message(chat_id, '> تم إلغاء الاشتراك الإجباري\\.')
        elif call.data == 'menu_admins':
            markup = types.InlineKeyboardMarkup()
            markup.add(btn("إضافة أدمن", callback_data='add_admin', emoji_id=E['check'], style="success"), btn("عرض الأدمنية", callback_data='show_admins', emoji_id=E['people'], style="primary"))
            markup.add(btn("رجوع", callback_data='back_admin', emoji_id=E['arrow'], style="primary"))
            bot.edit_message_text('> قائمة خيارات إداري البوت:', chat_id, call.message.message_id, reply_markup=markup)
        elif call.data == 'add_admin':
            msg = bot.send_message(chat_id, '> دز أيدي \\(ID\\) الأدمن الجديد:')
            bot.register_next_step_handler(msg, save_new_admin)
        elif call.data == 'show_admins':
            with db_lock:
                cursor.execute('SELECT user_id FROM admins'); admin_list = [str(r[0]) for r in cursor.fetchall()]
            bot.send_message(chat_id, '> قائمة الأدمنية:\n' + '\n'.join(admin_list))
        elif call.data == 'set_fixed_img':
            msg = bot.send_message(chat_id, '> دز الصورة الثابتة للترحيب بكل المستخدمين:')
            bot.register_next_step_handler(msg, save_fixed_image)

# ==========================================
# معالجة الصور المستلمة (الحد اليومي + الحماية)
# ==========================================
@bot.message_handler(content_types=['photo'])
def handle_user_photo(message):
    user_id = message.from_user.id
    if is_banned(user_id):
        bot.send_message(message.chat.id, '> 🚫 تم حظر حسابك من استخدام البوت\\.')
        return
    if not is_admin(user_id) and get_setting('maintenance_mode', '0') == '1':
        bot.send_message(message.chat.id, '> 🔧 البوت تحت الصيانة حالياً\\.')
        return
    if not check_sub(user_id):
        bot.send_message(message.chat.id, '> اشترك بالقناة أولاً حتى تقدر تدز صور للتقييم\\.')
        return
        
    current_time = int(time.time())
    today = date.today().isoformat()
    
    with db_lock:
        cursor.execute('SELECT last_photo_time, daily_count, last_submit_date FROM users WHERE user_id=?', (user_id,))
        row = cursor.fetchone()
        last_time = row[0] if row else 0
        daily_count = row[1] if row else 0
        last_submit_date = row[2] if row else today
        
        if last_submit_date != today:
            daily_count = 0
            
        # الأدمن معفى من الحد اليومي والوقت
        if not is_admin(user_id):
            if daily_count >= 3:
                bot.send_message(message.chat.id, '> ⚠️ وصلت الحد الأقصى لليوم \\(3 صور\\)\\.\n> عد غداً حتى تقدر تدز صور جديدة\\.')
                return
            if current_time - last_time < 30:
                bot.send_message(message.chat.id, f'> ⏱️ عد بعد {30 - (current_time - last_time)} ثانية حتى تقدر تدز صورة ثانية\\.')
                return

        cursor.execute('UPDATE users SET last_photo_time=?, daily_count=?, last_submit_date=? WHERE user_id=?', 
                       (current_time, daily_count + 1, today, user_id))
        conn.commit()
        
    photo_id = message.photo[-1].file_id
    user_temp_photos[user_id] = {'photo_id': photo_id}
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        btn("ولد", callback_data='gender_boy', emoji_id=E['sparkles'], style="primary"),
        btn("بنت", callback_data='gender_girl', emoji_id=E['sparkles'], style="primary")
    )
    bot.send_message(message.chat.id, '> يرجى اختيار جنس صاحب الصورة للتقييم الصحيح:', reply_markup=markup)

# ==========================================
# دوال الحفظ والمعالجة
# ==========================================
def process_unban(message):
    try:
        target_id = int(message.text)
        with db_lock:
            cursor.execute('UPDATE users SET banned=0, nsfw_warnings=0 WHERE user_id=?', (target_id,))
            conn.commit()
        bot.send_message(message.chat.id, f'> ✅ تم إلغاء حظر المستخدم `{target_id}` وتصفير تحذيراته\\.')
    except:
        bot.send_message(message.chat.id, '> ⚠️ الأيدي غير صحيح\\.')

def process_broadcast(message):
    sent = 0; failed = 0
    with db_lock:
        cursor.execute('SELECT user_id FROM users'); users = cursor.fetchall()
    bot.send_message(message.chat.id, f'> جاري الإرسال لـ {len(users)} مستخدم\\.\\.\\.')
    for (uid,) in users:
        try: bot.send_message(uid, message.text); sent += 1; time.sleep(0.05)
        except: failed += 1
    bot.send_message(message.chat.id, f'> ✅ تم الإرسال\\!\n> نجح: {sent}\n> فشل: {failed}')

def save_dev(message): set_setting('dev_user', message.text); bot.send_message(message.chat.id, f'> تم حفظ حساب المطور بنجاح: {escape_md(message.text)}')
def save_source(message): set_setting('source_channel', message.text); bot.send_message(message.chat.id, f'> تم حفظ قناة السورس بنجاح: {escape_md(message.text)}')
def save_target_channel(message): set_setting('target_channel', message.text); bot.send_message(message.chat.id, f'> تم ربط قناة التقييم بنجاح\\.\n\n> القناة المربوطة حالياً: {escape_md(message.text)}')
def save_sub_channel(message): set_setting('sub_channel', message.text); bot.send_message(message.chat.id, f'> تم حفظ قناة الاشتراك الإجباري بنجاح\\.\n\n> القناة الحالية: {escape_md(message.text)}')
def save_new_admin(message):
    try:
        new_id = int(message.text)
        with db_lock: cursor.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (new_id,)); conn.commit()
        bot.send_message(message.chat.id, '> تم إضافة الأدمن الجديد بنجاح\\.')
    except: bot.send_message(message.chat.id, '> الأيدي غير صحيح، يرجى كتابة أرقام فقط\\.')
def save_fixed_image(message):
    if message.photo: set_setting('fixed_image_id', message.photo[-1].file_id); bot.send_message(message.chat.id, '> تم تثبيت الصورة للترحيب بنجاح\\.')
    else: bot.send_message(message.chat.id, '> هذه مو صورة، يرجى إرسال صورة حصراً\\.')

# ==========================================
# نقطة التشغيل (حل مشكلة التعارض 409 Conflict)
# ==========================================
def run_bot():
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=20, skip_pending=True)
        except Exception as e:
            print(f"Bot Polling Error: {e}. Retrying in 10 seconds...")
            time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)).start()
    if APP_URL: threading.Thread(target=keep_alive_ping, daemon=True).start()
    run_bot()
