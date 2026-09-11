from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot import keyboards, texts
from database import crud

router = Router(name="recordings")


@router.callback_query(F.data == "menu:recordings")
async def cb_recordings(call: CallbackQuery) -> None:
    records = await crud.list_user_recordings(call.from_user.id, limit=10)
    if not records:
        await call.message.edit_text(texts.NO_RECORDINGS, reply_markup=keyboards.back_to_menu())
        await call.answer()
        return

    lines = ["<b>Последние записи</b>\n"]
    for rec in records:
        chat = await crud.get_watched_chat(rec.chat_id)
        title = chat.title if chat else str(rec.chat_id)
        m, s = divmod(rec.duration_seconds, 60)
        available = "✅" if rec.file_path else "🗑 удалена"
        lines.append(f"#{rec.id} · {title} · {m}:{s:02d} · {rec.started_at:%d.%m %H:%M} · {available}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    b = InlineKeyboardBuilder()
    for rec in records:
        if rec.file_path:
            b.button(text=f"📥 #{rec.id}", callback_data=f"rec:send:{rec.id}")
    b.button(text="◀️ В меню", callback_data="menu:root")
    b.adjust(4)

    await call.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("rec:send:"))
async def cb_send_recording(call: CallbackQuery) -> None:
    from bot.delivery import deliver_recording

    rec_id = int(call.data.split(":")[2])
    rec = await crud.get_recording(rec_id)
    if not rec or rec.started_by_id != call.from_user.id:
        await call.answer("Запись не найдена", show_alert=True)
        return
    await call.answer("Отправляю…")
    await deliver_recording(rec)
