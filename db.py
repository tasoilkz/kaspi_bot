import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

DB_PATH = "bot.db"
FREE_LIMIT = 3        # бесплатных расчётов на пользователя
SUB_DAYS = 30         # длительность подписки (1 месяц)
PENDING_HOURS = 24    # сколько часов заявка считается «висящей»

SETTING_FIELDS = {"regime", "margin_pct", "packaging", "delivery"}


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                free_used INTEGER NOT NULL DEFAULT 0,
                sub_until TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                amount       INTEGER NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                admin_msg_id INTEGER,
                created_at   TEXT NOT NULL,
                resolved_at  TEXT
            )
            """
        )
        # Настройки клиента живут отдельно от подписки: не сбрасываются
        # ни при окончании подписки, ни при повторной оплате.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_id    INTEGER PRIMARY KEY,
                regime     TEXT NOT NULL,
                margin_pct TEXT NOT NULL,
                packaging  TEXT NOT NULL,
                delivery   TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS last_calc (
                user_id    INTEGER PRIMARY KEY,
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                admin_msg_id INTEGER,
                created_at   TEXT NOT NULL
            )
            """
        )
        # Запоминаем, для какого срока подписки напоминание уже отправлено
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                user_id   INTEGER PRIMARY KEY,
                sub_until TEXT NOT NULL
            )
            """
        )


# ---------- Пользователи и подписки ----------
def _get_or_create(user_id: int) -> sqlite3.Row:
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        return c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def get_status(user_id: int) -> dict:
    row = _get_or_create(user_id)
    until = datetime.fromisoformat(row["sub_until"]) if row["sub_until"] else None
    return {
        "active": bool(until and until > _now()),
        "until": until,
        "free_left": max(0, FREE_LIMIT - row["free_used"]),
    }


def has_access(user_id: int) -> bool:
    """Доступ есть, если подписка не истекла или остались бесплатные расчёты.
    Когда месяц заканчивается, доступ закрывается сам."""
    s = get_status(user_id)
    return s["active"] or s["free_left"] > 0


def register_use(user_id: int) -> None:
    """Списывает бесплатный расчёт (у подписчиков ничего не списывается)."""
    if get_status(user_id)["active"]:
        return
    with _conn() as c:
        c.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id = ?", (user_id,))


def grant_subscription(user_id: int, days: int = SUB_DAYS) -> datetime:
    """Продлевает подписку: если она ещё активна, дни добавляются к остатку."""
    row = _get_or_create(user_id)
    current = datetime.fromisoformat(row["sub_until"]) if row["sub_until"] else None
    base = max(_now(), current) if current else _now()
    new_until = base + timedelta(days=days)
    with _conn() as c:
        c.execute("UPDATE users SET sub_until = ? WHERE user_id = ?", (new_until.isoformat(), user_id))
    return new_until


# ---------- Настройки клиента ----------
def get_settings(user_id: int):
    """Возвращает настройки клиента или None, если он ещё не проходил настройку."""
    with _conn() as c:
        row = c.execute("SELECT * FROM settings WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return {
        "regime": row["regime"],
        "margin_pct": Decimal(row["margin_pct"]),
        "packaging": Decimal(row["packaging"]),
        "delivery": Decimal(row["delivery"]),
    }


def save_settings(user_id: int, regime: str, margin_pct, packaging, delivery) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO settings (user_id, regime, margin_pct, packaging, delivery)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                regime = excluded.regime,
                margin_pct = excluded.margin_pct,
                packaging = excluded.packaging,
                delivery = excluded.delivery
            """,
            (user_id, regime, str(margin_pct), str(packaging), str(delivery)),
        )


def update_setting(user_id: int, field: str, value) -> None:
    if field not in SETTING_FIELDS:
        raise ValueError(f"Неизвестное поле: {field}")
    with _conn() as c:
        c.execute(f"UPDATE settings SET {field} = ? WHERE user_id = ?", (str(value), user_id))


# ---------- Последний расчёт (для отчётов об ошибках) ----------
def save_last_calc(user_id: int, text: str) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO last_calc (user_id, text, created_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET text = excluded.text, created_at = excluded.created_at
            """,
            (user_id, text, _now().isoformat()),
        )


def get_last_calc(user_id: int):
    with _conn() as c:
        row = c.execute("SELECT text FROM last_calc WHERE user_id = ?", (user_id,)).fetchone()
    return row["text"] if row else None


# ---------- Отчёты об ошибках ----------
def create_report(user_id: int) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO reports (user_id, created_at) VALUES (?, ?)",
            (user_id, _now().isoformat()),
        )
        return cur.lastrowid


def set_report_admin_msg(report_id: int, admin_msg_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE reports SET admin_msg_id = ? WHERE id = ?", (admin_msg_id, report_id))


def get_report_by_admin_msg(admin_msg_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM reports WHERE admin_msg_id = ? ORDER BY id DESC LIMIT 1",
            (admin_msg_id,),
        ).fetchone()


# ---------- Заявки на оплату ----------
def create_request(user_id: int, amount: int) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO payments (user_id, amount, created_at) VALUES (?, ?, ?)",
            (user_id, amount, _now().isoformat()),
        )
        return cur.lastrowid


def set_admin_msg(req_id: int, admin_msg_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE payments SET admin_msg_id = ? WHERE id = ?", (admin_msg_id, req_id))


def get_request(req_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM payments WHERE id = ?", (req_id,)).fetchone()


def get_request_by_admin_msg(admin_msg_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM payments WHERE admin_msg_id = ? ORDER BY id DESC LIMIT 1",
            (admin_msg_id,),
        ).fetchone()


def get_pending_request(user_id: int):
    """Свежая необработанная заявка пользователя (не старше PENDING_HOURS)."""
    cutoff = (_now() - timedelta(hours=PENDING_HOURS)).isoformat()
    with _conn() as c:
        return c.execute(
            "SELECT * FROM payments WHERE user_id = ? AND status = 'pending' "
            "AND created_at > ? ORDER BY id DESC LIMIT 1",
            (user_id, cutoff),
        ).fetchone()


def resolve_request(req_id: int, status: str) -> bool:
    """Закрывает заявку, только если она ещё pending. Возвращает True при успехе.
    Защита от двойного нажатия «Подтвердить» (иначе подписка выдалась бы дважды)."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE payments SET status = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
            (status, _now().isoformat(), req_id),
        )
        return cur.rowcount == 1


# ---------- Напоминания об окончании подписки ----------
def get_users_to_remind(days: int = 3):
    """Пользователи, у которых подписка закончится в ближайшие `days` дней
    и которым напоминание по ЭТОМУ сроку ещё не отправляли.
    Возвращает список (user_id, дата_окончания, строка_срока)."""
    now = _now()
    limit = now + timedelta(days=days)
    with _conn() as c:
        rows = c.execute(
            """
            SELECT u.user_id, u.sub_until
            FROM users u
            LEFT JOIN reminders r ON r.user_id = u.user_id
            WHERE u.sub_until IS NOT NULL
              AND (r.sub_until IS NULL OR r.sub_until != u.sub_until)
            """
        ).fetchall()
    result = []
    for row in rows:
        until = datetime.fromisoformat(row["sub_until"])
        if now < until <= limit:
            result.append((row["user_id"], until, row["sub_until"]))
    return result


def mark_reminded(user_id: int, sub_until_raw: str) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO reminders (user_id, sub_until) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET sub_until = excluded.sub_until
            """,
            (user_id, sub_until_raw),
        )
