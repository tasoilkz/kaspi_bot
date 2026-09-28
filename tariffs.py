from decimal import Decimal

AUTOTOVARY_RATE = Decimal("0.125")

TARIFFS = {
    "🛠 Автоинструменты": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🔐 Автопротивоугонные устройства": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "✨ Автохимия и автокосметика": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🔧 Автозапчасти": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🛢 Масла и технические жидкости": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🧰 Автосервисное оборудование": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🏭 Автомобильное оборудование": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🚘 Автоаксессуары": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "💡 Автомобильное освещение": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🔊 Автоакустика": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "📱 Автоэлектроника": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🧳 Багажные системы": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🛞 Шины": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "⚙️ Комплекты дисков": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🎨 Защита и внешний тюнинг": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🏍 Мототехника": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
    "🚜 Спецтехника": {"commission_without_vat": Decimal("0.109"), "commission_gross": AUTOTOVARY_RATE},
}

# Объёмы для масел/технических жидкостей.
OIL_VOLUMES = ["1 л", "4 л", "5 л", "20 л"]
