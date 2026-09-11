from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PromoStates(StatesGroup):
    waiting_for_code = State()


class WorkerLoginStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class WorkerProfileStates(StatesGroup):
    waiting_first_name = State()
    waiting_bio = State()
    waiting_photo = State()


class ConnectStates(StatesGroup):
    waiting_invite_link = State()
