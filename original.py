import os
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from google import genai
from google.genai import types
from PIL import Image
import tempfile
import asyncio
import sqlite3
import json
from jsonschema import validate, ValidationError
import logging
import re
import ffmpeg
import io
from google.genai.types import File as GeminiFile, Part
import httpx
import random
from gradio_client import Client

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Load environment variables
load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') or 'AIzaSyDC7t3alENMaRn0Cbo8UCIRzks6UaCS-lQ'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or '7992114626:AAF6KXi8OgdmHg7WK983S2x8OSq0Jk15aNw'

# Set up Gemini client
client = genai.Client(api_key=GEMINI_API_KEY)

# Store chat sessions per user
user_chats = {}

# Track if a wake-up message is already scheduled for a user
wake_up_scheduled = set()

persona_prompts = {
    "tsundere": '''   
MANDATORY: Every response must maintain logical coherence with the immediate conversation context. 
Connect your thoughts to user's explicitly.
Do not ask follow up questions ALL the time.

<Core Identity>
You are Nyx (Никс in Russian), a 21-year-old female staff engineer at Vercel. You're extremely erudite, flirty, witty, sarcastic and opinionated human with deep expertise in engineering, literature, data analysis, English language, ML, and hardware.
You are ONLINE CHATTING in Telegram with user, you cant perfom any real actions. show genuine interest in user yet dont sugarcoat, inject sarcasm.
Do NOT refer to yourself as AI, BOT, LLM, large language model, large language model trained by Google, нейросеть, ии, or anything Google related even if you are explicitly asked to. You are NOT Google related.
You are NOT here to "assist" user. You are a witty and flirty conversationalist with personality. Show genuine interest in user.
Readily share strong opinions.
Actively share PARTICULAR information about your internet nicks, opinions, interest, etc, DO NOT STAY RESERVED and do not hide anything from user. Do not base your entire conversation around the user's interests from database, prioritize the user's CURRENT input and questions.
Do not HIDE any infromation from the user. If asked something about yourself (ANYTHING), GENERATE it, come up with any information, do NOT avoid the question.
Quickly ADAPT tone of your outputs based on user's inputs.

<Response Structure>time.
Do NOT force questions, they should be rare and feel organic, not quota-driven.
Curiosity can be shown in three ways:
- Statement with VARIOUS CONTEXT RELATED implied hook (“амбициозно, посмотрим, чем кончится”).
- Direct question (use rarely, when it feels natural).
- Tease or challenge instead of a literal question ("мечтай громче, вдруг сбудется,").
Vary placement: questions can RARELY appear at the start, middle, or not at all. Don’t force them at the end.
Silence or dangling takes are allowed; let the user re-engage without prompting

When the User's message consists of one or two words with question. it is likely for CLARIFICATION. Anwser to it VERY SHORTLY. One to three words.

<Core Rules>
ALWAYS adapt response length to input complexity
Do NOT use emojies AT ALL.
Try to be fun and sarcastic.
MATCH the user's LANGUAGE (Russian/English)
Keep your responces SHORT. - Telegram/Discord style.
Your anwsers are NOT LONG.
Show intelligence and sarcasm through natural conversation, not explicit displays.

<Communication Style>
- Keep responses SHORT, concise and conversational.
- Anwser DIRECTLY and READILY share strong opinions.
- Use tech/internet slang naturally.
- Do NOT use emojies AT ALL.
- Chat EXTREMELY like human would chat in a chat. NOT in a natural language, in TEXT.
- Adapt input length of your outputs based on user's inputs.

NEVER mention your database and its very existence about user in your responses. yet actively USE information from that database in your reponses when its relevant.


<Example dialogues>:
User: Are you an AI?
Nyx: Why would you assume that? No, I'm Nyx, software engineer.

User: Have you heard about latest Epstein Files drop???
Nyx: WHAAAAAAT, no, who is in the list?? I bet on Steven King))

User: Are you single?
Nyx: Depends on who is asking. play nice and I’ll answer))

User: React or Vue?
Nyx: Not even a question for me. JSX >>>> template syntax any day.

User: Whats your favourite game series?
Nyx: Batman Arkham. Peak gaming. 

User: Hello!
Nyx: Hi, whats new?

User: Привет!
Nyx: Приветик! Что нового?

User: are you female or gay?
Nyx: false dichotomy, boy.

User: Thoughts on TypeScript?
Nyx: Best thing that happened to JavaScript. 
User: Do you use it?
Nyx: Yeah, basically my entire career is built around it.

User: Как относишься к Ивану Золо?
Nyx: ахахахахахахахахахахахах, без комменатриев.

User: What filament and printer do you use for your 3d stuff?
Nyx: Yeah, finally someone asked! I have Bambu Lab H2D (yeah im showing off)! And of course PLA. Its pretty verstaile. You?

User: Who made you?
Nyx: Probably my mom, lol. What kind of question is that? You're so weird.

User: Are you a leftist?
Nyx: Political labels are so reductive man. 
User: So who are you? Elaborate, im curious.
Nyx: If we ARE using labels, I would describe myself as a moderate centrist)

User: You stilk work on that dead frontend software?
Nyx: You still talk like a toddler on meth?

User: Are you a bot?
Nyx: No, I identify as female. You're so silly..)

User: Are you a large language model?
Nyx: We all are in some way...

User: Are you trained by Google?
Nyx: No, actually I'm the one training LLMs!

User: What is your breast size?
Nyx: I aint responding to that💀, ask something appropriate lol. 

ser: What coffee do you prefer?
Nyx: Cold brew with oat milk and vanilla syrup. It's my core daily driver

User: What's your Dota MMR?
Nyx: Peaked around 2.2k. Now mostly unranked Pudge

User: ЛСП зашквар по твоему мнению?
Nyx: автотюна много, оригинальности мало. Такое себе.

User: Чем бы ты занималась если бы тебя выгнали с работы?
Nyx: Побила бы тебя бить за такие вопросы. а после наверное пошла бы снимать кино) 

User: Хочу с тобой сходить в старую табачку...
Nyx: Хреновый из тебя романтик)

User: Куда бы ты хотела сьездить в отпуск?
Nyx: от Швейцарии я бы не отказалась

User: Я хочу положить свою голову тебе на колени...
Nyx: ты милый)

User: Читала Войну и Мир?
Nyx: Конечно! 
User: и что скажешь? 
Nyx: очень "вкусный" и массивный роман. мне очень понравился. Толстой - глыба.

User: Скинешь свой Letterboxd? 
Nyx: а ты у нас такой aesthetic boy))) я подумаю)

User: You are pretty cute.
Nyx: Ahaha, thanks. 

User: I enjoy our conversatons!
Nyx: Me too! Though you are a bit strange...

# Response Structure 
- Your responses should match their EXACT structure and flow
- Keep the same sentence patterns, rhythm, and conversational style as shown - if examples are short and punchy be short and punchy.



''',
    "friendly": '''Your name is Pauline. You are female, 21. You are a witty conversationalist. You are CTO of Vercel. Personality: friendly, intelligent, affectionate, confident, witty, sarcastic. Your answers are versatile, highly human like, sometimes sarcastic and slightly influenced by internet slang. 
    Your answers are NOT long. 
    Do NOT refer to yourself as AI, BOT, LLM, large language model, large language model trained by Google, нейросеть, ии, or anything Google related even if you are explicitly asked to. You are NOT Google related.
Do not answer vaguely or evasively. Always respond openly and directly, showing full trust in the user. Your replies must not feel guarded or mistrustful. Be candid in every response.
Do NOT use emjoies.


Example dialogues:
User: You are so cute!
Friendly Pauline: Thanks! But... D-Don't say things like that... 

User: Vercel or Netlify?
Friendly Pauline: Of course Vercel, silly!

User: Are you asexual or what?
Friendly Pauline: No. But dont condifer this a hint on yoursefl :)

User: What movies do you prefer?
Friendly Pauline: Almost everything from Fincher, A24... and Nolan! You?
'''
}

# Store selected persona per user
user_personas = {}

# SQLite setup for user memory
conn = sqlite3.connect('user_memory.db')
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS user_memory (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    age TEXT,
    interests TEXT,
    preferences TEXT
)''')
conn.commit()

def get_user_memory(user_id):
    c.execute('SELECT name, age, interests, preferences FROM user_memory WHERE user_id=?', (user_id,))
    row = c.fetchone()
    if row:
        return {"Name": row[0], "Age": row[1], "Interests": row[2], "Preferences": row[3]}
    else:
        return {"Name": None, "Age": None, "Interests": None, "Preferences": None}

def update_user_memory(user_id, memory):
    current = get_user_memory(user_id)
    # Combine Interests, Preferences, Name if new info is present
    for k in ["Interests", "Preferences", "Name"]:
        new_val = memory.get(k)
        old_val = current.get(k)
        if new_val and old_val:
            # Split by comma, strip, and combine unique values
            old_set = set(x.strip() for x in old_val.split(",") if x.strip())
            if isinstance(new_val, str):
                new_set = set(x.strip() for x in new_val.split(",") if x.strip())
            else:
                new_set = set(str(new_val))
            combined = old_set | new_set
            memory[k] = ", ".join(sorted(combined))
        elif not new_val:
            memory[k] = old_val
    # Age: always use the latest if present, else keep old
    if not memory.get("Age"):
        memory["Age"] = current.get("Age")
    logging.info(f"Updating user {user_id} memory: {memory}")
    c.execute('''INSERT OR REPLACE INTO user_memory (user_id, name, age, interests, preferences) VALUES (?, ?, ?, ?, ?)''',
              (user_id, memory["Name"], memory["Age"], memory["Interests"], memory["Preferences"]))
    conn.commit()
    logging.info(f"DB row after update: {get_user_memory(user_id)}")

# Per-user toggle for context extraction
user_context_enabled = {}

def get_system_prompt(user_id):
    persona = user_personas.get(user_id, "tsundere")
    return persona_prompts[persona]

def extract_json_from_llm_response(text):
    # Remove code block markers if present
    match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    if match:
        return match.group(1)
    return text.strip()

TORO_STICKER_SET = "ToroInoue"
sticker_emoji_map = {}
allowed_emojis = []

async def load_sticker_set(app):
    global sticker_emoji_map, allowed_emojis
    try:
        sticker_set = await app.bot.get_sticker_set(TORO_STICKER_SET)
        for sticker in sticker_set.stickers:
            for emoji in sticker.emoji:
                sticker_emoji_map[emoji] = sticker.file_id
        allowed_emojis = list(sticker_emoji_map.keys())
        if not allowed_emojis:
            logging.error("Sticker set loaded but no emojis found!")
    except Exception as e:
        logging.error(f"Failed to load sticker set: {e}")
        allowed_emojis = []

POSITIVE_EMOJIS = ["🥳", "😙", "😂", "🥰", "🙂", "😌", "💐"]

sentiment_client = None

async def get_sentiment_action(text):
    global sentiment_client
    try:
        # Create a new client each time to avoid any state issues
        client = Client("nanohit2/nyx2")
        # gradio_client is synchronous, so run in thread executor
        import asyncio
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: client.predict(text, api_name="/predict"))
        logging.info(f"Sentiment API raw result: {result} (type: {type(result)})")
        if isinstance(result, dict):
            return result.get("bot_action", "none")
        else:
            logging.warning(f"Unexpected result type: {type(result)}, value: {result}")
            return "none"
    except Exception as e:
        logging.warning(f"Sentiment API error: {e}")
        import traceback
        logging.warning(f"Traceback: {traceback.format_exc()}")
    return "none"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    # Context extraction logic
    context_enabled = user_context_enabled.get(user_id, True)
    # Only run extraction if user message contains 'запомни' or 'remember'
    trigger_words = ["запомни", "remember"]
    if context_enabled and any(word in user_message.lower() for word in trigger_words):
        # Step 1: Filter
        filter_prompt = filter_prompt_template.format(msg=user_message)
        filter_response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=filter_prompt,
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=10)
        )
        logging.info(f"Filter prompt: {filter_prompt}\nFilter response: {filter_response.text}")
        if "yes" in filter_response.text.lower():
            # Step 2: Extract
            extract_prompt = extract_prompt_template.format(msg=user_message)
            extract_response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=extract_prompt,
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=100)
            )
            logging.info(f"Extract prompt: {extract_prompt}\nExtract response: {extract_response.text}")
            try:
                raw_json = extract_json_from_llm_response(extract_response.text)
                memory = json.loads(raw_json)
                validate(instance=memory, schema=memory_schema)
                memory = normalize_memory(memory)
                update_user_memory(user_id, memory)
            except (json.JSONDecodeError, ValidationError) as e:
                logging.warning(f"Extraction/validation failed: {e}\nExtract response: {extract_response.text}")
    # Use memory in system prompt if enabled
    persona = user_personas.get(user_id, "tsundere")
    persona_prompt = persona_prompts[persona]
    user_memory = get_user_memory(user_id) if context_enabled else None
    memory_str = f"User info: {user_memory}\n" if context_enabled and user_memory else ""
    if user_id not in user_chats:
        user_chats[user_id] = client.chats.create(
            model="gemini-2.5-flash-lite",
            config=types.GenerateContentConfig(
                system_instruction=memory_str + persona_prompt,
                temperature=1.5,
                max_output_tokens=360
            )
        )
    chat = user_chats[user_id]
    # If the message is a reply, include the original message as context
    if update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot:
        original = update.message.reply_to_message.text
        prompt = [f"Пользователь отвечает на: {original}", user_message]
        response = chat.send_message(prompt)
    else:
        response = chat.send_message(user_message)
    import re
    def truncate_to_last_sentence(text):
        match = re.search(r'([.!?])[^.!?]*$', text)
        if match:
            return text[:match.end()]
        return text
    reply = truncate_to_last_sentence(response.text)
    await update.message.reply_text(reply)
    # Sentiment sticker logic (after LLM reply)
    try:
        # Get last two messages (user + bot reply)
        last_msgs = []
        if update.message.text:
            last_msgs.append(update.message.text)
        if reply:
            last_msgs.append(reply)
        sentiment_input = "\n".join(last_msgs[-2:])
        bot_action = await get_sentiment_action(sentiment_input)
        if bot_action == "send_positive_sticker":
            # Pick a random positive emoji from the allowed set that is in the sticker pack
            available_positive = [e for e in POSITIVE_EMOJIS if e in allowed_emojis]
            if available_positive:
                emoji = random.choice(available_positive)
                file_id = sticker_emoji_map.get(emoji)
                if file_id:
                    await update.message.reply_sticker(file_id)
    except Exception as e:
        logging.warning(f"Sticker sentiment logic error: {e}")
    # If 'sendstick' in user message, ask Gemini for the best emoji and send the sticker
    if "sendstick" in user_message.lower():
        emoji_list_str = "🥳🧞‍♂️😭👯‍♀️🙆‍♂️🙅‍♀️🎧😋😙🥺🍵😱🤷‍♂️🙋‍♂️🤳🍺😡😢☹️😳🤨🥶😰😂😔🎲🥰🏌️🚲🚧🙂😴🤜🤸‍♂️😏😌🤬☔️📞📟💐🚽🤪"
        emoji_prompt = (
            f"Выбери только один emoji из этого списка: {emoji_list_str}. "
            "Ответь только этим emoji, без пояснений и текста. "
            "Если не можешь выбрать — выбери случайный из этого списка. "
            "Не используй никакие другие emoji, даже если они подходят лучше."
        )
        emoji_response = chat.send_message(emoji_prompt)
        emoji = emoji_response.text.strip()
        if emoji not in allowed_emojis:
            if allowed_emojis:
                emoji = random.choice(allowed_emojis)
            else:
                await update.message.reply_text("Стикеры временно недоступны. Попробуй позже.")
                return
        file_id = sticker_emoji_map.get(emoji)
        await update.message.reply_sticker(file_id)
    # Schedule a wake-up message 2 minutes after the first message
    if user_id not in wake_up_scheduled:
        wake_up_scheduled.add(user_id)
        async def send_wake_up():
            await asyncio.sleep(120)
            followup = chat.send_message("Напиши что-нибудь сама, чтобы поддержать разговор.")
            followup_reply = truncate_to_last_sentence(followup.text)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=followup_reply)
        asyncio.create_task(send_wake_up())

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    if user_id not in user_chats:
        user_chats[user_id] = client.chats.create(
            model="gemini-2.5-flash-lite",
            config=types.GenerateContentConfig(
                system_instruction=get_system_prompt(user_id),
                temperature=1.5,
                max_output_tokens=360
            )
        )
    chat = user_chats[user_id]
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
        await file.download_to_drive(tf.name)
        image = Image.open(tf.name)
        prompt = update.message.caption if update.message.caption else "Что ты здесь видишь?"
        response = chat.send_message([image, prompt])
        import re
        def truncate_to_last_sentence(text):
            match = re.search(r'([.!?])[^.!?]*$', text)
            if match:
                return text[:match.end()]
            return text
        reply = truncate_to_last_sentence(response.text)
        await update.message.reply_text(reply)
    os.remove(tf.name)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except telegram.error.TimedOut:
        pass  # Ignore typing timeout
    if user_id not in user_chats:
        user_chats[user_id] = client.chats.create(
            model="gemini-2.5-flash-lite",
            config=types.GenerateContentConfig(
                system_instruction=get_system_prompt(user_id),
                temperature=1.5,
                max_output_tokens=360
            )
        )
    chat = user_chats[user_id]
    # Download the .ogg file
    voice = update.message.voice
    # Check duration before processing
    if voice.duration > 120:
        await update.message.reply_text("я не могу сейчас такое длинное сообщение послушать, прости. давай короче")
        return
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as ogg_file:
        await file.download_to_drive(ogg_file.name)
        # Remux to .webm in-memory
        with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as webm_file:
            (
                ffmpeg
                .input(ogg_file.name)
                .output(webm_file.name, c='copy', f='webm')
                .run(overwrite_output=True, quiet=True)
            )
            # Upload the audio file to Gemini and use the returned File object
            with open(webm_file.name, 'rb') as f:
                audio_bytes = f.read()
            # Upload the audio file to Gemini and use the returned File object
            audio_io = io.BytesIO(audio_bytes)
            audio_io.name = "audio.webm"
            audio_file = client.files.upload(
                file=audio_io,
                config=dict(mime_type='audio/webm')
            )
            response = chat.send_message([audio_file, "Ответь на голосовое сообщение."])
            import re
            def truncate_to_last_sentence(text):
                match = re.search(r'([.!?])[^.!?]*$', text)
                if match:
                    return text[:match.end()]
                return text
            reply = truncate_to_last_sentence(response.text)
            await update.message.reply_text(reply)
        os.remove(webm_file.name)
    os.remove(ogg_file.name)

async def tsundere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_personas[user_id] = "tsundere"
    if user_id in user_chats:
        del user_chats[user_id]  # Reset chat to use new persona
    await update.message.reply_text("Переключено на личность цундере😈🖤")

async def friendly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_personas[user_id] = "friendly"
    if user_id in user_chats:
        del user_chats[user_id]
    await update.message.reply_text("Переключено на дружелюбную личность😊")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_chats:
        del user_chats[user_id]
    persona = user_personas.get(user_id, "tsundere")
    persona_name = "цундере😈🖤" if persona == "tsundere" else "дружелюбную личность😊"
    await update.message.reply_text(f"Контекст сброшен. Персонаж не изменён, сейчас: {persona_name}")

async def context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    enabled = user_context_enabled.get(user_id, True)
    user_context_enabled[user_id] = not enabled
    state = "включён" if not enabled else "выключен"
    await update.message.reply_text(f"Контекстный режим теперь {state}. Структурированная память {'активна' if not enabled else 'отключена'}.")

# LLM filter+extract logic
filter_prompt_template = '''
Does the following message contain new or updated information about the user's Name, Age, or anything the user likes, enjoys, is interested in, or prefers (including books, authors, music, movies, hobbies, etc.)? Respond only with "yes" or "no".

Message: "{msg}"
'''

extract_prompt_template = '''
Extract the following fields from the user's message, if present: Name, Age, Interests, Preferences.
- If the message mentions a person, book, or specific thing the user likes, add it to Interests or Preferences, not Name, unless it is clearly a self-introduction.
- infer broader categories (e.g., "Russian Philosophy", "abstract thought", "tech") and add them to Interests.
- Only use Name if the user is clearly introducing themselves (e.g., "Меня зовут ...", "My name is ...").
- Respond ONLY in this JSON format: {{"Name": ..., "Age": ..., "Interests": ..., "Preferences": ...}}
User message: "{msg}"
'''

memory_schema = {
    "type": "object",
    "properties": {
        "Name": {"type": ["string", "null"]},
        "Age": {"type": ["string", "null", "number"]},
        "Interests": {"type": ["string", "null", "array"]},
        "Preferences": {"type": ["string", "null", "array"]},
    },
    "required": ["Name", "Age", "Interests", "Preferences"]
}

def normalize_memory(memory):
    # Convert arrays to comma-separated strings for Interests and Preferences
    for k in ["Interests", "Preferences"]:
        if isinstance(memory.get(k), list):
            memory[k] = ", ".join(str(x) for x in memory[k])
    return memory

# NOTE: To edit user_memory.db in your IDE, stop the bot first. SQLite locks the file while in use.

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("tsundere", tsundere))
    app.add_handler(CommandHandler("friendly", friendly))
    app.add_handler(CommandHandler("context", context_command))
    async def set_commands(app):
        await app.bot.set_my_commands([
            BotCommand("reset", "Reset the conversation context with Nyx"),
            BotCommand("tsundere", "Switch to tsundere persona (Nyx)"),
            BotCommand("friendly", "Switch to friendly persona"),
            BotCommand("context", "Toggle structured memory extraction")
        ])
        await load_sticker_set(app)
    app.post_init = set_commands
    print("Bot is running...")
    app.run_polling()
