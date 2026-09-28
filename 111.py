import os
from datetime import datetime
import pandas as pd
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler

# Наш калькулятор налогов для ОУР и анализатор отчетов TASOIL
from calculator import calculate_prices
from analyzer import parse_1c_mxl_excel

# ==========================================
# 1. НАСТРОЙКИ
# ==========================================
TELEGRAM_BOT_TOKEN = "8546823881:AAH4zoXE-ZiKiE1ql4vglxU8FmH9u54JzzY"
MY_CHAT_ID = 1889997265

BUTTON_GENERATE = "📊 Сформировать прайс Kaspi"
BUTTON_STATUS = "ℹ️ Статус системы"
BUTTON_ANALYZE = "📊 Анализ TASOIL"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BUTTON_GENERATE, BUTTON_ANALYZE], [BUTTON_STATUS]],
    resize_keyboard=True
)

# ==========================================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (КАСПИ)
# ==========================================
def clean_vendor_code(val) -> str:
    """Сохраняет артикул ровно в том виде, в котором он указан в файле (с нулями)"""
    if pd.isna(val):
        return ""
    art = str(val).strip()
    if art.endswith(".0"):
        art = art[:-2]
    return art

def get_1c_file_path():
    """Универсальный поиск файла отчета 1С"""
    possible_folders = ["1c", "1с", "1C", "1С"]
    
    for folder in possible_folders:
        if os.path.exists(folder):
            for file in os.listdir(folder):
                if file.lower().endswith(('.xls', '.xlsx')):
                    return os.path.join(folder, file)
            
    for file in os.listdir("."):
        if file.lower().endswith(('.xls', '.xlsx')) and "ostatok" in file.lower():
            return file
            
    return None

def get_stock_and_price_from_1c(art, df_1c):
    """Парсит структуру отчета 1С"""
    art = clean_vendor_code(art)
    if not art or df_1c is None:
        return 0, 0.0

    for i in range(len(df_1c)):
        col0 = str(df_1c.iloc[i, 0])
        col1 = str(df_1c.iloc[i, 1])

        if col1 == 'БУ' and art in col0:
            stock = 0
            price = 0.0

            if i + 1 < len(df_1c) and str(df_1c.iloc[i + 1, 1]) == 'Кол.':
                qty_row = df_1c.iloc[i + 1]
                val_stock = qty_row.iloc[6]
                if pd.notna(val_stock):
                    stock = int(val_stock)
                else:
                    start_q = qty_row.iloc[2] if pd.notna(qty_row.iloc[2]) else 0
                    in_q = qty_row.iloc[4] if pd.notna(qty_row.iloc[4]) else 0
                    out_q = qty_row.iloc[5] if pd.notna(qty_row.iloc[5]) else 0
                    stock = int(start_q + in_q - out_q)

            bu_row = df_1c.iloc[i]
            val_price = bu_row.iloc[6]
            if pd.notna(val_price):
                price = float(val_price)
            else:
                start_p = bu_row.iloc[2] if pd.notna(bu_row.iloc[2]) else 0
                in_p = bu_row.iloc[4] if pd.notna(bu_row.iloc[4]) else 0
                out_p = bu_row.iloc[5] if pd.notna(bu_row.iloc[5]) else 0
                price = float(start_p + in_p - out_p)

            if stock > 0 and price > stock * 1000:
                price = price / stock

            return max(0, stock), round(price, 2)

    return 0, 0.0

# ==========================================
# 3. ОСНОВНАЯ ЛОГИКА СБОРКИ ПРАЙСА KASPI
# ==========================================
def generate_kaspi_price():
    """Собирает прайс Kaspi с учётом виртуальных остатков и без дублирования номенклатуры"""
    print("\n--- Запуск генерации прайса для Kaspi ---")
    kaspi_rows = []

    # А. Товары со СКЛАДА (mapping.xlsx)
    if os.path.exists("mapping.xlsx"):
        mapping_df = pd.read_excel("mapping.xlsx", dtype=str)
        active_mapping = mapping_df[mapping_df["Статус"] == "Вкл"] if "Статус" in mapping_df.columns else mapping_df

        ostatok_path = get_1c_file_path()
        df_1c = None
        if ostatok_path:
            try:
                df_1c = pd.read_excel(ostatok_path, sheet_name=0)
            except Exception as e:
                print(f"⚠️ Ошибка чтения файла 1С: {e}")

        for _, row in active_mapping.iterrows():
            art = clean_vendor_code(row.get("Артикул 1С", ""))
            kaspi_name = str(row.get("Наименование для Kaspi", "")).strip()
            
            raw_virt = row.get("Виртуальный остаток", 0)
            virt_stock = int(float(raw_virt)) if pd.notna(raw_virt) and str(raw_virt).strip() != "" else 0

            stock_1c, price_1c = get_stock_and_price_from_1c(art, df_1c)
            final_stock = stock_1c if stock_1c > 0 else virt_stock

            kaspi_rows.append({
                "VendorCode": art,
                "ProductName": kaspi_name,
                "Stock": final_stock,
                "Price": price_1c,
                "Type": "Склад",
            })

    # Б. Товары ПОД ЗАКАЗ (supplier_price.xlsx)
    if os.path.exists("supplier_price.xlsx"):
        supplier_df = pd.read_excel("supplier_price.xlsx", dtype=str)
        active_supplier = supplier_df[supplier_df["Статус"] == "Вкл"] if "Статус" in supplier_df.columns else supplier_df

        for _, row in active_supplier.iterrows():
            art = clean_vendor_code(row.get("Артикул", ""))
            kaspi_name = str(row.get("Наименование для Kaspi", "")).strip()
            
            stock_val = row.get("Виртуальный остаток", 0)
            stock = int(float(stock_val)) if pd.notna(stock_val) and str(stock_val).strip() != "" else 0
            
            purchase_price = float(row.get("Цена поставщика", 0))
            weight_kg = float(row.get("Вес (кг)", 1.0))
            commission_pct = float(row.get("Комиссия Kaspi (%)", 11.5))

            calc_result = calculate_prices(
                purchase_price=purchase_price,
                weight_kg=weight_kg,
                kaspi_commission_pct=commission_pct,
                target_margin_pct=15.0
            )

            kaspi_rows.append({
                "VendorCode": art,
                "ProductName": kaspi_name,
                "Stock": stock,
                "Price": calc_result["target_price"],
                "Type": "Под заказ",
            })

    # В. Убираем дубликаты по артикулу VendorCode
    df_final = pd.DataFrame(kaspi_rows)
    if not df_final.empty:
        df_final = df_final.drop_duplicates(subset=["VendorCode"], keep="first")

    # Г. Сохраняем в папку exports
    os.makedirs("exports", exist_ok=True)
    filename = f"Kaspi_Price_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    output_path = os.path.join("exports", filename)
    
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        df_final.to_excel(writer, index=False)

    print(f"✅ Успешно! Файл сохранен: {output_path}")
    return output_path

# ==========================================
# 4. АВТОМАТИКА И ОБРАБОТЧИКИ (ТЕЛЕГРАМ)
# ==========================================
def scheduled_job(application):
    ostatok_path = get_1c_file_path()
    if ostatok_path:
        file_path = generate_kaspi_price()
        try:
            with open(file_path, "rb") as f:
                application.bot.send_document(
                    chat_id=MY_CHAT_ID,
                    document=f,
                    caption="📊 Автоматический прайс для Kaspi успешно сформирован!",
                )
        except Exception as e:
            print(f"⚠️ Ошибка отправки документа в Telegram: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != MY_CHAT_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    await update.message.reply_text(
        f"Привет! Бот TASOIL запущен и готов к работе. 🚀\n"
        f"Твой Chat ID: `{chat_id}`",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != MY_CHAT_ID:
        return

    text = update.message.text
    if text == BUTTON_GENERATE:
        await update.message.reply_text("⏳ Собираю актуальный прайс для Kaspi...", reply_markup=MAIN_KEYBOARD)
        try:
            file_path = generate_kaspi_price()
            with open(file_path, "rb") as f:
                await update.message.reply_document(
                    document=f, 
                    caption="📊 Готово! Прайс-лист для Kaspi с учётом НДС 16% и КПН 10%.",
                    reply_markup=MAIN_KEYBOARD
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Произошла ошибка при сборке: {e}", reply_markup=MAIN_KEYBOARD)

    elif text == BUTTON_ANALYZE:
        await update.message.reply_text(
            "📂 Отправьте мне файл отчёта из 1С (Excel), и я проведу финансовый анализ продаж, прибыли и налогов.",
            reply_markup=MAIN_KEYBOARD
        )

    elif text == BUTTON_STATUS:
        ostatok_path = get_1c_file_path()
        file_name = os.path.basename(ostatok_path) if ostatok_path else ""
        has_1c = f"✅ Найден (`{file_name}`)" if ostatok_path else "❌ Отсутствует"
        has_mapping = "✅ Найден" if os.path.exists("mapping.xlsx") else "❌ Отсутствует"
        has_supplier = "✅ Найден" if os.path.exists("supplier_price.xlsx") else "❌ Отсутствует"

        msg = (
            f"⚙️ *Статус системы TASOIL:*\n\n"
            f"• Отчет 1С: {has_1c}\n"
            f"• Белый список (`mapping.xlsx`): {has_mapping}\n"
            f"• Прайс поставщика (`supplier_price.xlsx`): {has_supplier}\n\n"
            f"⏰ Налоги: ОУР (НДС 16% + КПН 10%)."
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

async def handle_documents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != MY_CHAT_ID:
        return

    document = update.message.document
    if not document:
        return

    file_name = document.file_name.lower()
    if not (file_name.endswith('.xls') or file_name.endswith('.xlsx')):
        await update.message.reply_text("⚠️ Пожалуйста, отправьте файл отчета в формате Excel (.xls или .xlsx).", reply_markup=MAIN_KEYBOARD)
        return

    await update.message.reply_text("⏳ Скачиваю и анализирую отчёт...", reply_markup=MAIN_KEYBOARD)

    try:
        file = await context.bot.get_file(document.file_id)
        local_path = "sales_report.xlsx"
        await file.download_to_drive(local_path)

        # Запускаем парсер из analyzer.py
        df = parse_1c_mxl_excel(local_path)

        if df.empty:
            await update.message.reply_text("⚠️ Не удалось извлечь данные из отчета. Проверьте структуру файла.", reply_markup=MAIN_KEYBOARD)
            return

        total_revenue = df['revenue'].sum()
        total_profit = df['profit'].sum()
        total_qty = df['qty'].sum()

        # Расчет налогов общей прибыли (НДС 16% + КПН 10%)
        total_profit_net = total_profit * (1 - 0.16) * (1 - 0.10)

        report_lines = [
            "📊 *АНАЛИТИЧЕСКИЙ ОТЧЕТ ПО ПРОДАЖАМ TASOIL*",
            "--------------------------------------------------",
            f"📦 **Всего продано единиц:** `{int(total_qty)} шт.`",
            f"💰 **Общая выручка:** `{total_revenue:,.2f} ₸`".replace(',', ' '),
            f"📈 **Общая чистая прибыль (до вычета):** `{total_profit:,.2f} ₸`".replace(',', ' '),
            f"📉 **Чистая прибыль с учетом налогов (НДС 16% + КПН 10%) (с вычетом):** `{total_profit_net:,.2f} ₸`".replace(',', ' '),
            "--------------------------------------------------",
            "🏆 *ПО ПОЗИЦИЯМ (Продажи и Прибыль):*"
        ]

        df_profit = df.sort_values(by='profit', ascending=False)
        for idx, row in df_profit.iterrows():
            name_short = row['name'][:40] + ('...' if len(row['name']) > 40 else '')
            qty_val = int(row['qty']) if 'qty' in row and pd.notna(row['qty']) else 0
            
            p_before = row['profit']
            p_after = p_before * (1 - 0.16) * (1 - 0.10)
            
            profit_before_str = f"{p_before:,.2f} ₸".replace(',', ' ')
            profit_after_str = f"{p_after:,.2f} ₸".replace(',', ' ')
            revenue_str = f"{row['revenue']:,.2f} ₸".replace(',', ' ')
            
            report_lines.append(
                f"• `{name_short}`\n"
                f"  Продано: *{qty_val} шт.* | Прибыль (до вычета): *{profit_before_str}*\n"
                f"  Прибыль (с вычетом): *{profit_after_str}* | Выручка: {revenue_str}"
            )

        report_lines.append("\n✅ *Анализ успешно завершен!*")

        final_message = "\n".join(report_lines)
        await update.message.reply_text(final_message, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

    except Exception as e:
        await update.message.reply_text(f"❌ Произошла ошибка при обработке файла: {e}", reply_markup=MAIN_KEYBOARD)

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_documents))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: scheduled_job(app), "cron", hour=17, minute=0, timezone="Asia/Almaty")
    scheduler.add_job(lambda: scheduled_job(app), "cron", hour=18, minute=0, timezone="Asia/Almaty")
    scheduler.start()

    print("🤖 Бот TASOIL соединен с Kaspi и расширенной аналитикой запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()