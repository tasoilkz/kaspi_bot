from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

VAT = Decimal("0.16")
BUFFER = Decimal("1.03")  # запас 3% на операционные расходы

# Режимы налогообложения.
#   vat=True  -> плательщик НДС: продажа и комиссия Kaspi считаются без НДС,
#                входной НДС по закупке возмещается, налог берётся с ПРИБЫЛИ.
#   vat=False -> без НДС: закупка и комиссия Kaspi учитываются целиком,
#                налог берётся с ОБОРОТА (с цены продажи).
REGIMES = {
    "ip_simple": {
        "title": "ИП · упрощёнка (4%)",
        "vat": False,
        "tax_rate": Decimal("0.04"),
        "tax_label": "Налог (4% с оборота)",
    },
    "ip_general": {
        "title": "ИП · общеустановленный (НДС 16%, ИПН 10%)",
        "vat": True,
        "tax_rate": Decimal("0.10"),
        "tax_label": "ИПН (10% с прибыли)",
    },
    "too_general": {
        "title": "ТОО · общеустановленный (НДС 16%, КПН 20%)",
        "vat": True,
        "tax_rate": Decimal("0.20"),
        "tax_label": "КПН (20% с прибыли)",
    },
}


@dataclass
class Calculation:
    regime: str
    purchase_gross: Decimal
    purchase_net: Decimal
    input_vat: Decimal
    kaspi_rate: Decimal
    delivery: Decimal
    packaging: Decimal
    tax: Decimal
    minimum_price: Decimal
    recommended_price: Decimal
    net_profit: Decimal
    margin: Decimal


def round_up_100(value: Decimal) -> Decimal:
    return (
        value / Decimal("100")
    ).to_integral_value(rounding=ROUND_CEILING) * Decimal("100")


def calculate_price(
    purchase_gross: Decimal,
    kaspi_rate: Decimal,
    regime_key: str,
    target_margin: Decimal,
    delivery: Decimal,
    packaging: Decimal,
) -> Calculation:
    """target_margin — доля (0.20 = 20%) чистой прибыли от цены продажи.
    Бросает ValueError, если такая маржа при этом режиме недостижима."""
    regime = REGIMES[regime_key]
    t = regime["tax_rate"]
    fixed = delivery + packaging
    one = Decimal("1")

    if regime["vat"]:
        one_plus_vat = one + VAT
        purchase_net = purchase_gross / one_plus_vat
        input_vat = purchase_gross - purchase_net

        # Прибыль до налога = P * revenue_factor - закупка без НДС - фикс. расходы
        revenue_factor = (one - kaspi_rate) / one_plus_vat
        denominator = (one - t) * revenue_factor - target_margin
        if denominator <= 0:
            raise ValueError("Целевая маржа недостижима.")

        minimum_price = (one - t) * (purchase_net + fixed) / denominator
        pretax = minimum_price * revenue_factor - purchase_net - fixed
        tax = pretax * t
        net_profit = pretax - tax
    else:
        purchase_net = purchase_gross
        input_vat = Decimal("0")

        denominator = one - kaspi_rate - t - target_margin
        if denominator <= 0:
            raise ValueError("Целевая маржа недостижима.")

        minimum_price = (purchase_gross + fixed) / denominator
        tax = minimum_price * t
        net_profit = minimum_price * (one - kaspi_rate) - purchase_gross - fixed - tax

    margin = net_profit / minimum_price
    recommended_price = round_up_100(minimum_price * BUFFER)

    return Calculation(
        regime=regime_key,
        purchase_gross=purchase_gross,
        purchase_net=purchase_net,
        input_vat=input_vat,
        kaspi_rate=kaspi_rate,
        delivery=delivery,
        packaging=packaging,
        tax=tax,
        minimum_price=minimum_price,
        recommended_price=recommended_price,
        net_profit=net_profit,
        margin=margin,
    )


def money(value):
    value = value.quantize(Decimal("0.01"))
    return f"{value:,.2f}".replace(",", " ")
