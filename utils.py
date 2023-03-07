# SPDX-License-Identifier: MIT

# Copyright (c) 2023 scmanjarrez. All rights reserved.
# This work is licensed under the terms of the MIT license.

import json

import asyncio
import edge_tts
import logging
import re
import tempfile
import os

from dateutil.parser import isoparse
from EdgeGPT import Chatbot
from telegram import (
    constants,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import ContextTypes

FILE = {
    "cfg": ".config.json",
    "cookies": ".cookies.json",
    "allowed": ".allowed.txt",
}
DATA = {"cfg": None, "allowed": []}
CONV = {}
REF = re.compile(r"\[\^(\d+)\^\]")
CODE = re.compile(r"(?<!\()(?:```(.*?)```|`(.*?)`)")
BOLD = re.compile(r"(?:\*\*(.*?)\*\*|__(.*?)__)")
ITA = re.compile(r"(?<!\()(?:\*(.*?)\*|_(.*?)_)")


def set_up() -> None:
    with open(FILE["cfg"]) as f:
        DATA["cfg"] = json.load(f)
    try:
        with open(FILE["allowed"]) as f:
            DATA["allowed"] = [int(_cid) for _cid in f.read().splitlines()]
    except FileNotFoundError:
        DATA["allowed"] = []
    try:
        logging.getLogger().setLevel(settings("log_level").upper())
    except KeyError:
        pass


def save_cfg() -> None:
    with open(FILE["cfg"], "w") as f:
        json.dump(DATA["cfg"], f, indent=2)


def settings(key: str) -> str:
    return DATA["cfg"]["settings"][key]


def passwd_correct(passwd: str) -> bool:
    return passwd == DATA["cfg"]["chats"]["password"]


def allowed(update: Update) -> bool:
    _cid = cid(update)
    return _cid in DATA["allowed"] or _cid in DATA["cfg"]["chats"]["id"]


def cid(update: Update) -> int:
    return update.effective_chat.id


def unlock(chat_id: int) -> None:
    DATA["allowed"].append(chat_id)
    with open(FILE["allowed"], "a") as f:
        f.write(f"{chat_id}\n")


def is_group(update: Update) -> bool:
    return update.effective_chat.id < 0


def reply_markup(buttons: list) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(bt)] for bt in buttons])


def inline_markup(buttons: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(bt[0], bt[1])] for bt in buttons]
    )


async def _remove_conversation(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    await CONV[job.chat_id].close()
    del CONV[job.chat_id]


def delete_job(context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    current_jobs = context.job_queue.get_jobs_by_name(name)
    for job in current_jobs:
        job.schedule_removal()


def delete_conversation(
    context: ContextTypes.DEFAULT_TYPE, name: str, expiration: str
) -> None:
    delete_job(context, name)
    context.job_queue.run_once(
        _remove_conversation,
        isoparse(expiration),
        chat_id=int(name),
        name=name,
    )


async def send(update: Update, text, quote=False, reply_markup=None) -> None:
    return await update.effective_message.reply_html(
        text,
        disable_web_page_preview=True,
        quote=quote,
        reply_markup=reply_markup,
    )


async def generate_voice(text, voice="zh-CN-YunjianNeural") -> str:
    voice_text = REF.sub("", text)
    voice_text = BOLD.sub("", voice_text)
    logging.info(voice_text)
    OUTPUT_FILE = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    communicate = edge_tts.Communicate(voice_text, voice)
    await communicate.save(OUTPUT_FILE.name)
    return OUTPUT_FILE.name


async def send_voice(update: Update, voice: str) -> None:
    with open(voice, "rb") as f:
        await update.effective_message.reply_voice(f)
        os.remove(voice)


async def show_voice_name(update: Update) -> None:
    await update.effective_message.reply_text(settings("voice"))


async def set_voice_name(update: Update) -> None:
    voice = update.effective_message.text.split("voice ")[1]
    DATA["cfg"]["settings"]["voice"] = voice
    save_cfg()
    await show_voice_name(update)


async def is_active_conversation(
    update: Update, new=False, finished=False
) -> bool:
    _cid = cid(update)
    if new or finished or _cid not in CONV:
        if _cid in CONV:
            await CONV[_cid].close()
        try:
            CONV[_cid] = Chatbot(cookiePath=FILE["cookies"])
        except Exception as e:
            logging.getLogger("EdgeGPT").error(e)
            await send(update, "EdgeGPT API not available. Try again later.")
            return False
        else:
            missing = "Conversation expired. "
            group = "Reply to any of my messages to interact with me."
            await send(
                update,
                (
                    f"{missing if not new or finished else ''}"
                    "Starting new conversation. "
                    f"{'Ask me anything... ' if new else ''}"
                    f"{group if is_group(update) else ''}"
                ),
                reply_markup=ReplyKeyboardRemove(),
            )
    return True


async def send_typing(context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(
        context.job.chat_id, constants.ChatAction.TYPING
    )


def typing_schedule(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    _cid = cid(update)
    context.job_queue.run_repeating(
        send_typing, 7, first=1, chat_id=_cid, name=f"typing_{_cid}"
    )


class Query:
    def __init__(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self.update = update
        self.context = context

    async def run(self) -> None:
        _cid = cid(self.update)
        typing_schedule(self.update, self.context)
        self._response = await CONV[cid(self.update)].ask(
            self.update.effective_message.text
        )
        delete_job(self.context, f"typing_{_cid}")
        item = self._response["item"]
        if item["result"]["value"] == "Success":
            self.expiration = item["conversationExpiryTime"]
            delete_conversation(self.context, str(_cid), self.expiration)
            finished = True
            for message in item["messages"]:
                if message["author"] == "bot":
                    await self.parse_message(message)
                    finished = False
            if finished:
                await is_active_conversation(self.update, finished=finished)
                query = Query(self.update, self.context)
                await query.run()
        else:
            logging.getLogger("EdgeGPT").error(item["result"]["error"])
            msg = "EdgeGPT API not available. Try again later."
            if item["result"]["value"] == "Throttled":
                msg = (
                    "Reached Bing chat daily quota. Try again tomorrow, sorry!"
                )
            await send(self.update, msg)

    def markdown_to_html(self, text: str) -> str:
        code = []
        not_code = []
        last = 0
        for itr in CODE.finditer(text):
            code.append(
                CODE.sub(
                    "<code>\\1\\2</code>", text[itr.start(0) : itr.end(0)]
                )
            )
            not_code.append(text[last : itr.start(0)])
            last = itr.end(0)
        not_code.append(text[last:])
        for idx, sub in enumerate(not_code):
            new = BOLD.sub("<b>\\1\\2</b>", sub)
            new = ITA.sub("<i>\\1\\2</i>", new)
            not_code[idx] = new
        added = 0
        for idx, cc in enumerate(code, 1):
            not_code.insert(added + idx, cc)
            added += 1
        return "".join(not_code)

    async def parse_message(self, message: dict) -> None:
        def generate_link(match: re.Match) -> str:
            text = match.group(1)
            link = f" [{text}]"
            if text in references:
                link = f"<a href='{references[text]}'> [{text}]</a>"
            return link

        logger = logging.getLogger("EdgeGPT")
        logger.info(message)

        text = self.markdown_to_html(message["text"])
        extra = ""

        if "sourceAttributions" in message:
            references = {
                str(idx): ref["seeMoreUrl"]
                for idx, ref in enumerate(message["sourceAttributions"], 1)
            }
            full_ref = [
                f'<a href="{url}">[{idx}]</a>'
                for idx, url in references.items()
            ]
            if references:
                extra = f"\n\n<b>References</b>: {' '.join(full_ref)}"
            text = REF.sub(generate_link, text)
        bt_list = ["/new"]
        if "suggestedResponses" in message and not is_group(self.update):
            bt_list = [
                sug["text"] for sug in message["suggestedResponses"]
            ] + bt_list
        suggestions = reply_markup(bt_list)
        await send(
            self.update, f"{text}{extra}", reply_markup=suggestions, quote=True
        )

        if settings("reply_voice"):
            voice = settings("voice")
            voice_file = await generate_voice(message["text"], voice=voice)
            await send_voice(self.update, voice=voice_file)
