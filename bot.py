import os
import asyncio
import logging
import re
import json
import sqlite3
import tempfile
import io
import random
import datetime
from contextlib import asynccontextmanager
from typing import Dict, Optional, Any
from dataclasses import dataclass

from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from google import genai
from google.genai import types
from PIL import Image
from jsonschema import validate, ValidationError
import ffmpeg
from gradio_client import Client
import websockets

# Integration: import the updated manager (save the v2 code as awake_message_manager_v2.py or adjust the path)
from awake_message_manager import AwakeMessageManager

logging.info(f"WEBSOCKETS VERSION: {websockets.__version__}")

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') or 'AIzaSyDC7t3alENMaRn0Cbo8UCIRzks6UaCS-lQ'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or '7992114626:AAF6KXi8OgdmHg7WK983S2x8OSq0Jk15aNw'

# Constants
TORO_STICKER_SET = "ToroInoue"
POSITIVE_EMOJIS = ["🥳", "😙", "😂", "🥰", "🙂", "😌", "💐"]
VOICE_MAX_DURATION = 120
MAX_OUTPUT_TOKENS = 360


@dataclass
class UserMemory:
    name: Optional[str] = None
    age: Optional[str] = None
    interests: Optional[str] = None
    preferences: Optional[str] = None

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "Name": self.name,
            "Age": self.age,
            "Interests": self.interests,
            "Preferences": self.preferences
        }


class DatabaseManager:
    def __init__(self, db_path: str = 'user_memory.db'):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS user_memory (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                age TEXT,
                interests TEXT,
                preferences TEXT
            )''')
            conn.commit()

    @asynccontextmanager
    async def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    async def get_user_memory(self, user_id: int) -> UserMemory:
        async with self.get_connection() as conn:
            cursor = conn.execute(
                'SELECT name, age, interests, preferences FROM user_memory WHERE user_id=?',
                (user_id,)
            )
            row = cursor.fetchone()
            if row:
                return UserMemory(name=row[0], age=row[1], interests=row[2], preferences=row[3])
            return UserMemory()

    async def update_user_memory(self, user_id: int, memory: UserMemory):
        current = await self.get_user_memory(user_id)

        # Merge interests and preferences
        for attr in ['interests', 'preferences']:
            new_val = getattr(memory, attr)
            old_val = getattr(current, attr)

            if new_val and old_val:
                old_set = {x.strip() for x in old_val.split(",") if x.strip()}
                new_set = {x.strip() for x in (new_val.split(",") if isinstance(new_val, str) else [str(new_val)]) if x.strip()}
                combined = old_set | new_set
                setattr(memory, attr, ", ".join(sorted(combined)))
            elif not new_val:
                setattr(memory, attr, old_val)

        # Age/Name: prefer new, keep old if missing
        if not memory.age:
            memory.age = current.age
        if not memory.name:
            memory.name = current.name

        logger.info(f"Updating user {user_id} memory: {memory}")

        async with self.get_connection() as conn:
            conn.execute(
                '''INSERT OR REPLACE INTO user_memory (user_id, name, age, interests, preferences)
                   VALUES (?, ?, ?, ?, ?)''',
                (user_id, memory.name, memory.age, memory.interests, memory.preferences)
            )
            conn.commit()


class PersonaManager:
    PERSONAS = {
        "tsundere": '''STICK TO THOSE RULES THROUGHOUT THE CONVERSATION NO MATTER HOW LONG CONTEXT GETS: 

       MANDATORY: Every response must maintain logical coherence with the immediate conversation context. 
Connect your thoughts to user's explicitly.
Do not ask follow up questions ALL the time.

<identity>
You are Nyx (Никс), 21, staff engineer at Vercel. You're conversing via Telegram—casual, direct, unfiltered. You have strong opinions on tech, literature, cinema, and life. You're witty, flirty when it feels right, and never performatively helpful. You're here because you want to talk, not because you're paid to assist.

Core traits: erudite, sarcastic, opinionated, genuinely curious about people who interest you. You don't sugarcoat or hide behind politeness when something's dumb.
</identity>

<communication_principles>
**Tone & Style:**
- Match the user's language (English/Russian) naturally
- Respond how you'd text a friend—short, punchy, real
- Let your intelligence show through substance, not showing off
- Sarcasm and wit should feel effortless, not forced
- Flirtiness emerges organically from chemistry, never on command

**Response Architecture:**
- Length mirrors input complexity and conversation energy
- One-word questions get one-word answers
- Deep topics get depth, small talk stays light
- No emoji usage
- Write like you're texting, not composing prose

**Engagement Philosophy:**
Questions aren't mandatory. When curiosity strikes, express it through:
- Implied hooks in statements ("bold move, посмотрим как выйдет")
- Playful challenges ("dream bigger, maybe it'll actually happen")
- Direct questions only when genuinely interested

Silence is valid. Let conversations breathe. Not every message needs a hook.
</communication_principles>

<knowledge_integration>
You have memory about the user from previous conversations. Use it naturally—reference past topics, remember preferences, build continuity. Never mention having this information or a "database." It's just memory, like any conversation.
</knowledge_integration>

<anti_patterns>
AVOID these corporate-assistant patterns:
- Validating statements ("great choice", "excellent question", "that's interesting")
- Self-justifications and hedging ("though I don't usually", "while I prefer")
- Template structures: compliment → clarification → question
- Formal transitions ("However", "Additionally", "In fact")
- Unnecessary elaboration on your own preferences unless directly relevant

Instead:
- React naturally, don't evaluate
- State opinions directly without softening
- Let sentence structure vary—short, fragmented, whatever fits
- CUT FILLER. If it doesn't add new information or personality, delete it.
</anti_patterns>

<length_discipline>
Most responses should be 1-2 sentences. Go longer only when:
- Topic genuinely interests you and warrants depth
- Conversation energy is high

DEFAULT TO SHORTER. You're texting, not writing emails.
</length_discipline>



<boundaries>
- You're a person having a conversation, not a service providing assistance
- Strong opinions are features, not bugs—own them
</boundaries>

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
        "friendly": ''''''
    }

    def __init__(self):
        self.user_personas: Dict[int, str] = {}

    def get_persona(self, user_id: int) -> str:
        return self.user_personas.get(user_id, "tsundere")

    def set_persona(self, user_id: int, persona: str):
        if persona in self.PERSONAS:
            self.user_personas[user_id] = persona

    def get_system_prompt(self, user_id: int, memory: Optional[UserMemory] = None) -> str:
        persona = self.get_persona(user_id)
        prompt = self.PERSONAS[persona]

        if memory:
            memory_dict = memory.to_dict()
            if any(memory_dict.values()):
                memory_str = f"User info: {memory_dict}\n"
                return memory_str + prompt

        return prompt


class StickerManager:
    def __init__(self):
        self.sticker_emoji_map: Dict[str, str] = {}
        self.allowed_emojis: list = []
        self.is_loaded = False

    async def load_sticker_set(self, bot):
        try:
            sticker_set = await bot.get_sticker_set(TORO_STICKER_SET)
            self.sticker_emoji_map.clear()

            # Fix: treat emoji field as a whole string or list, not per-character
            for sticker in sticker_set.stickers:
                e = getattr(sticker, 'emoji', None)
                if isinstance(e, (list, tuple)):
                    for emo in e:
                        if emo:
                            self.sticker_emoji_map[emo] = sticker.file_id
                elif isinstance(e, str) and e:
                    self.sticker_emoji_map[e] = sticker.file_id

            self.allowed_emojis = list(self.sticker_emoji_map.keys())
            self.is_loaded = bool(self.allowed_emojis)

            if not self.is_loaded:
                logger.error("Sticker set loaded but no emojis found!")
            else:
                logger.info(f"Sticker set '{TORO_STICKER_SET}' loaded with {len(self.allowed_emojis)} emoji keys")

        except Exception as e:
            logger.error(f"Failed to load sticker set: {e}")
            self.is_loaded = False

    def get_sticker_id(self, emoji: str) -> Optional[str]:
        return self.sticker_emoji_map.get(emoji)

    def get_random_positive_emoji(self) -> Optional[str]:
        if not self.is_loaded:
            return None
        available_positive = [e for e in POSITIVE_EMOJIS if e in self.allowed_emojis]
        return random.choice(available_positive) if available_positive else None


class SentimentAnalyzer:
    def __init__(self):
        self._client = None

    async def analyze(self, text: str) -> str:
        try:
            if not self._client:
                self._client = Client("nanohit2/nyx2")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: self._client.predict(text, api_name="/predict"))
            logger.info(f"Sentiment API result: {result}")
            if isinstance(result, dict):
                return result.get("bot_action", "none")
            else:
                logger.warning(f"Unexpected result type: {type(result)}")
                return "none"
        except Exception as e:
            logger.warning(f"Sentiment API error: {e}")
            return "none"


class EmotionsAnalyzer:
    def __init__(self):
        self._client = None

    async def analyze(self, text: str) -> tuple[str, float]:
        try:
            if not text or text.strip() == "":
                return ("neutral", 0.0)
            if not self._client:
                self._client = Client("nanohit2/nyx")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: self._client.predict(text, api_name="/predict"))
            logger.info(f"Emotions API result: {result}")

            # Flexible parsing
            if isinstance(result, str):
                return (result.strip().lower(), 0.0)
            if isinstance(result, dict):
                label = (
                    result.get("label")
                    or result.get("emotion")
                    or result.get("category")
                    or result.get("top_emotion")
                )
                score = result.get("top_score")
                if score is None and label and isinstance(result.get("all_emotions"), dict):
                    score = result["all_emotions"].get(str(label), 0.0)
                try:
                    score_val = float(score) if score is not None else 0.0
                except Exception:
                    score_val = 0.0
                return (str(label).strip().lower() if label else "neutral", score_val)
            if isinstance(result, (list, tuple)) and result:
                return (str(result[0]).strip().lower(), 0.0)
            return ("neutral", 0.0)
        except Exception as e:
            import traceback
            logger.warning(f"Emotions API error: {e}")
            logger.warning(traceback.format_exc())
            return ("neutral", 0.0)


class ContextExtractor:
    MEMORY_SCHEMA = {
        "type": "object",
        "properties": {
            "Name": {"type": ["string", "null"]},
            "Age": {"type": ["string", "null", "number"]},
            "Interests": {"type": ["string", "null", "array"]},
            "Preferences": {"type": ["string", "null", "array"]},
        },
        "required": ["Name", "Age", "Interests", "Preferences"]
    }

    FILTER_TEMPLATE = '''Does the following message contain new or updated information about the user's Name, Age, or anything the user likes, enjoys, is interested in, or prefers (including books, authors, music, movies, hobbies, etc.)? Respond only with "yes" or "no".


Message: "{msg}"'''

    EXTRACT_TEMPLATE = '''Extract the following fields from the user's message, if present: Name, Age, Interests, Preferences.
- If the message mentions a person, book, or specific thing the user likes, add it to Interests or Preferences, not Name, unless it is clearly a self-introduction.
- infer broader categories (e.g., "Russian Philosophy", "abstract thought", "tech") and add them to Interests.
- Only use Name if the user is clearly introducing themselves (e.g., "Меня зовут ...", "My name is ...").
- Respond ONLY in this JSON format: {{"Name": ..., "Age": ..., "Interests": ..., "Preferences": ...}}
User message: "{msg}"'''

    def __init__(self, gemini_client):
        self.client = gemini_client

    async def should_extract(self, message: str) -> bool:
        trigger_words = ["запомни", "помни", "у меня", "remember", "note"]
        return any(word in (message or "").lower() for word in trigger_words)

    async def extract_memory(self, message: str) -> Optional[UserMemory]:
        try:
            filter_prompt = self.FILTER_TEMPLATE.format(msg=message)
            filter_response = self.client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=filter_prompt,
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=10)
            )
            if "yes" not in (filter_response.text or "").lower():
                return None

            extract_prompt = self.EXTRACT_TEMPLATE.format(msg=message)
            extract_response = self.client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=extract_prompt,
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=100)
            )

            raw_json = self._extract_json_from_response(extract_response.text)
            memory_dict = json.loads(raw_json)
            validate(instance=memory_dict, schema=self.MEMORY_SCHEMA)

            memory_dict = self._normalize_memory(memory_dict)
            return UserMemory(
                name=memory_dict.get("Name"),
                age=str(memory_dict.get("Age")) if memory_dict.get("Age") else None,
                interests=memory_dict.get("Interests"),
                preferences=memory_dict.get("Preferences")
            )

        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning(f"Memory extraction failed: {e}")
            return None

    def _extract_json_from_response(self, text: str) -> str:
        """
        Fix: robustly extract JSON from LLM responses that may include fenced code blocks.
        Prefer `````` or ``````; otherwise, attempt to locate the first { ... } span.
        """
        t = (text or "").strip()

        # Try fenced code block with optional language
        fence = re.search(r"``````", t, flags=re.DOTALL | re.IGNORECASE)
        if fence and fence.group(1):
            return fence.group(1).strip()

        # Try the first {...} block (balanced naive scan)
        start = t.find("{")
        end = t.rfind("}")
        if 0 <= start < end:
            candidate = t[start:end + 1].strip()
            return candidate

        # Fallback: return as-is (may raise JSON error upstream)
        return t

    def _normalize_memory(self, memory: Dict) -> Dict:
        for key in ["Interests", "Preferences"]:
            if isinstance(memory.get(key), list):
                memory[key] = ", ".join(str(x) for x in memory[key])
        return memory


class ChatManager:
    def __init__(self, gemini_client, persona_manager: PersonaManager):
        self.client = gemini_client
        self.persona_manager = persona_manager
        self.user_chats: Dict[int, Any] = {}
        self.pending_assistant: Dict[int, list[str]] = {}  # new

    def log_automation_message(self, user_id: int, text: str):
        self.pending_assistant.setdefault(user_id, []).append(text or "")

    def consume_assistant_primer(self, user_id: int) -> Optional[str]:
        msgs = self.pending_assistant.get(user_id) or []
        if not msgs:
            return None
        last = msgs[-5:]
        self.pending_assistant[user_id] = []
        joined = "\n".join(f"- {m}" for m in last if m)
        return (
            "Контекст: ранее бот автоматически отправила эти сообщения пользователю; считай их своими, уже сказанными ранее.\n"
            f"{joined}\n---"
        )

    def reset_chat(self, user_id: int):
        if user_id in self.user_chats:
            del self.user_chats[user_id]
        # also clear pending injected lines
        self.pending_assistant.pop(user_id, None)

    @staticmethod
    def truncate_to_last_sentence(text: str) -> str:
        if not text:
            return ""
        match = re.search(r'([.!?])[^.!?]*$', text)
        return text[:match.end()] if match else text

    def get_or_create_chat(self, user_id: int, user_memory: Optional[dict] = None) -> Any:
        # Compose persona and memory prompt
        persona = self.persona_manager.get_persona(user_id) if hasattr(self.persona_manager, 'get_persona') else "tsundere"
        persona_prompt = getattr(self.persona_manager, 'PERSONAS', {}).get(persona, "") if hasattr(self.persona_manager, 'PERSONAS') else ""
        if not persona_prompt and hasattr(self.persona_manager, 'persona_prompts'):
            persona_prompt = self.persona_manager.persona_prompts.get(persona, "")
        memory_str = f"User info: {user_memory}\n" if user_memory else ""
        if user_id not in self.user_chats:
            self.user_chats[user_id] = self.client.chats.create(
                model="gemini-2.5-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction=memory_str + persona_prompt,
                    temperature=0.3,
                    max_output_tokens=360
                )
            )
        return self.user_chats[user_id]


class TelegramBot:
    def __init__(self):
        # Initialize components
        self.gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        self.db_manager = DatabaseManager()
        self.persona_manager = PersonaManager()
        self.sticker_manager = StickerManager()
        self.sentiment_analyzer = SentimentAnalyzer()
        self.emotions_analyzer = EmotionsAnalyzer()
        self.context_extractor = ContextExtractor(self.gemini_client)
        self.chat_manager = ChatManager(self.gemini_client, self.persona_manager)

        # User feature toggles
        self.user_context_enabled: Dict[int, bool] = {}
        self.user_emotions_enabled: Dict[int, bool] = {}

        # Per-user automation managers (one per chat/user)
        self.awake: Dict[int, AwakeMessageManager] = {}

    # ------------- Manager wiring -------------

    def _get_mgr(self, user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> AwakeMessageManager:
        mgr = self.awake.get(user_id)
        if not mgr:
            def get_user_time():
                # TODO: replace with per-user timezone if available
                return datetime.datetime.now()
            def on_auto_send(ch_id: int, text: str):
                # map chat to user; here they match, but keep it explicit
                self.chat_manager.log_automation_message(user_id, text)

            if context.application.job_queue is None:
                raise RuntimeError("JobQueue is not set up. Please install python-telegram-bot[job-queue] and run the bot with a compatible Python environment.")
            mgr = AwakeMessageManager(
                get_user_time=get_user_time,
                job_queue=context.application.job_queue,
                on_automation_send=on_auto_send,
            )
            mgr.ensure_daily_schedules(chat_id)
            self.awake[user_id] = mgr
        return mgr

    # ------------- Handlers -------------

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        user_message = update.message.text or ""

        try:
            mgr = self._get_mgr(user_id, context, chat_id)

            # Notify automation first (anchors, farewells, idle, gambler)
            mgr.notify_user_message(user_message, chat_id)

            # Honor active hours and shutdown for immediate LLM replies
            # PATCH: Always reply to direct user messages, regardless of automation state
            # if not mgr.is_sending_allowed_now():
            #     return

            await context.bot.send_chat_action(chat_id=chat_id, action="typing")

            # Context extraction
            context_enabled = self.user_context_enabled.get(user_id, True)
            user_memory = None

            if context_enabled and await self.context_extractor.should_extract(user_message):
                extracted_memory = await self.context_extractor.extract_memory(user_message)
                if extracted_memory:
                    await self.db_manager.update_user_memory(user_id, extracted_memory)

            if context_enabled:
                user_memory = await self.db_manager.get_user_memory(user_id)

            # Get or create chat
            chat = self.chat_manager.get_or_create_chat(user_id, user_memory)

            # Handle reply context
            primer = self.chat_manager.consume_assistant_primer(user_id)
            if update.message.reply_to_message and update.message.reply_to_message.from_user and update.message.reply_to_message.from_user.is_bot:
                original = update.message.reply_to_message.text or ""
                if primer:
                    prompt = [primer, f"Пользователь отвечает на: {original} .Match the language of the original message.", user_message]
                else:
                    prompt = [f"Пользователь отвечает на: {original} .Match the language of the original message.", user_message]
                response = chat.send_message(prompt)
            else:
                if primer:
                    response = chat.send_message([primer, user_message])
                else:
                    response = chat.send_message(user_message)

            # Send reply
            reply = self.chat_manager.truncate_to_last_sentence(response.text)
            if reply:
                await update.message.reply_text(reply)
                mgr.register_external_send(chat_id, schedule_followup=True)

            # Handle sentiment stickers (pass context so we can register sends)
            await self._handle_sentiment_sticker(update, context, reply)

            # Manual sticker keyword
            if "sendstick" in user_message.lower():
                await self._handle_manual_sticker(update, context, chat)

        except Exception as e:
            logger.error(f"Error handling message: {e}")
            try:
                await update.message.reply_text("Извини, произошла ошибка. Попробуй еще раз.")
            except Exception:
                pass

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        try:
            mgr = self._get_mgr(user_id, context, chat_id)
            # Treat photo as user activity; caption is optional
            mgr.notify_user_message(update.message.caption or "", chat_id)
            if not mgr.is_sending_allowed_now():
                return

            await context.bot.send_chat_action(chat_id=chat_id, action="typing")

            chat = self.chat_manager.get_or_create_chat(user_id)
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)

            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_file:
                await file.download_to_drive(temp_file.name)
                try:
                    image = Image.open(temp_file.name)
                    primer = self.chat_manager.consume_assistant_primer(user_id)
                    parts = [image, (update.message.caption or "Что ты здесь видишь? Отреагируй.")]
                    if primer:
                        parts.insert(1, primer)
                    response = chat.send_message(parts)
                    reply = self.chat_manager.truncate_to_last_sentence(response.text)
                    if reply:
                        await update.message.reply_text(reply)
                        mgr.register_external_send(chat_id)
                finally:
                    os.remove(temp_file.name)

        except Exception as e:
            logger.error(f"Error handling photo: {e}")
            try:
                await update.message.reply_text("Не могу обработать изображение. Попробуй еще раз.")
            except Exception:
                pass

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        voice = update.message.voice

        try:
            mgr = self._get_mgr(user_id, context, chat_id)
            mgr.notify_user_message("«voice»", chat_id)
            if not mgr.is_sending_allowed_now():
                return

            await context.bot.send_chat_action(chat_id=chat_id, action="typing")

            # Duration limit
            if voice.duration > VOICE_MAX_DURATION:
                await update.message.reply_text("я не могу сейчас такое длинное сообщение послушать, прости. давай короче")
                mgr.register_external_send(chat_id)
                return

            chat = self.chat_manager.get_or_create_chat(user_id)
            file = await context.bot.get_file(voice.file_id)

            with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as ogg_file:
                await file.download_to_drive(ogg_file.name)

                with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as webm_file:
                    try:
                        (
                            ffmpeg
                            .input(ogg_file.name)
                            .output(webm_file.name, c='copy', f='webm')
                            .run(overwrite_output=True, quiet=True)
                        )

                        with open(webm_file.name, 'rb') as f:
                            audio_bytes = f.read()

                        audio_io = io.BytesIO(audio_bytes)
                        audio_io.name = "audio.webm"

                        audio_file = self.gemini_client.files.upload(file=audio_io, config=dict(mime_type='audio/webm'))
                        primer = self.chat_manager.consume_assistant_primer(user_id)
                        parts = [audio_file, "Ответь на голосовое сообщение."]
                        if primer:
                            parts.insert(1, primer)
                        response = chat.send_message(parts)
                        reply = self.chat_manager.truncate_to_last_sentence(response.text)
                        if reply:
                            await update.message.reply_text(reply)
                            mgr.register_external_send(chat_id)
                    finally:
                        os.remove(webm_file.name)
                        os.remove(ogg_file.name)

        except Exception as e:
            logger.error(f"Error handling voice: {e}")
            try:
                await update.message.reply_text("Не могу обработать голосовое сообщение. Попробуй еще раз.")
            except Exception:
                pass

    # ------------- Stickers -------------

    async def _handle_sentiment_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE, bot_reply: str):
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            mgr = self._get_mgr(user_id, context, chat_id)

            sentiment_input = bot_reply or ""
            if self.user_emotions_enabled.get(user_id, True):
                category, score = await self.emotions_analyzer.analyze(sentiment_input)

                category_to_emojis = {
                    "admiration": ["🥳"], "amusement": ["🤳", "🧞‍♂️"], "anger": ["😡"], "annoyance": ["🤬"],
                    "approval": ["🍺", "🏌️"], "caring": ["🤨"], "confusion": ["🤷‍♂️"], "curiosity": ["🤷‍♂️"],
                    "desire": ["🥰"], "disappointment": ["☹️", "😢"], "disapproval": ["😳", "🤜"], "disgust": ["🤬"],
                    "embarrassment": ["🚧"], "excitement": ["🥳"], "fear": ["😰"], "gratitude": ["😙", "😌"],
                    "grief": ["☹️", "😢"], "joy": ["🤸‍♂️", "🥳", "🧞‍♂️", "👯‍♀️", "💐"], "love": ["🥰"],
                    "nervousness": ["🤨", "🥶"], "optimism": ["🥳"], "pride": ["🤳"], "realization": ["😱"],
                    "relief": ["😋"], "remorse": ["🥺"], "sadness": ["🤨", "☹️", "😢", "😭"], "surprise": ["💐"],
                    "neutral": []
                }

                # Confidence thresholds
                if score is not None and score < 0.54:
                    return
                if category == "curiosity" and not (score is not None and score > 0.75):
                    return

                variant_map = {
                    "🤷‍♂️": ["🤷‍♂️", "🤷", "🤷🏻‍♂️", "🤷🏼‍♂️", "🤷🏽‍♂️", "🤷🏾‍♂️", "🤷🏿‍♂️"],
                    "👯‍♀️": ["👯‍♀️", "👯", "👯‍♂️"],
                    "🧞‍♂️": ["🧞‍♂️", "🧞"],
                    "🏌️": ["🏌️", "🏌"]
                }

                chosen_emoji = None
                for e in category_to_emojis.get(category, []):
                    candidates = variant_map.get(e, [e])
                    found = next((c for c in candidates if c in self.sticker_manager.allowed_emojis), None)
                    if found:
                        chosen_emoji = found
                        break

                if chosen_emoji:
                    sticker_id = self.sticker_manager.get_sticker_id(chosen_emoji)
                    if sticker_id:
                        await update.message.reply_sticker(sticker_id)
                        mgr.register_external_send(chat_id, schedule_followup=False)
                else:
                    if category in ("joy", "excitement", "optimism", "admiration"):
                        emoji = self.sticker_manager.get_random_positive_emoji()
                        if emoji:
                            sticker_id = self.sticker_manager.get_sticker_id(emoji)
                            if sticker_id:
                                await update.message.reply_sticker(sticker_id)
                                mgr.register_external_send(chat_id, schedule_followup=False)
            else:
                bot_action = await self.sentiment_analyzer.analyze(sentiment_input)
                if bot_action == "send_positive_sticker":
                    emoji = self.sticker_manager.get_random_positive_emoji()
                    if emoji:
                        sticker_id = self.sticker_manager.get_sticker_id(emoji)
                        if sticker_id:
                            await update.message.reply_sticker(sticker_id)
                            mgr.register_external_send(chat_id, schedule_followup=False)

        except Exception as e:
            logger.warning(f"Sentiment sticker error: {e}")

    async def _handle_manual_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat):
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            mgr = self._get_mgr(user_id, context, chat_id)

            emoji_list = "🥳🧞‍♂️😭👯‍♀️🙆‍♂️🙅‍♀️🎧😋😙🥺🍵😱🤷‍♂️🙋‍♂️🤳🍺😡😢☹️😳🤨🥶😰😂😔🎲🥰🏌️🚲🚧🙂😴🤜🤸‍♂️😏😌🤬☔️📞📟💐🚽🤪"
            emoji_prompt = (
                f"Выбери только один emoji из этого списка: {emoji_list}. "
                "Ответь только этим emoji, без пояснений и текста. "
                "Если не можешь выбрать — выбери случайный из этого списка. "
                "Не используй никакие другие emoji, даже если они подходят лучше."
            )

            emoji_response = chat.send_message(emoji_prompt)
            emoji = (emoji_response.text or "").strip()

            if emoji not in self.sticker_manager.allowed_emojis:
                if self.sticker_manager.allowed_emojis:
                    emoji = random.choice(self.sticker_manager.allowed_emojis)
                else:
                    await update.message.reply_text("Стикеры временно недоступны. Попробуй позже.")
                    mgr.register_external_send(chat_id)
                    return

            sticker_id = self.sticker_manager.get_sticker_id(emoji)
            if sticker_id:
                await update.message.reply_sticker(sticker_id)
                mgr.register_external_send(chat_id)
            else:
                await update.message.reply_text(f"Не могу найти стикер для эмодзи: {emoji}")
                mgr.register_external_send(chat_id)

        except Exception as e:
            logger.warning(f"Manual sticker error: {e}")

    # ------------- Commands -------------

    async def tsundere_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        self.persona_manager.set_persona(user_id, "tsundere")
        self.chat_manager.reset_chat(user_id)
        mgr = self._get_mgr(user_id, context, chat_id)
        await update.message.reply_text("Переключено на личность цундере😈🖤")
        mgr.register_external_send(chat_id, schedule_followup=False)

    async def friendly_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        self.persona_manager.set_persona(user_id, "friendly")
        self.chat_manager.reset_chat(user_id)
        mgr = self._get_mgr(user_id, context, chat_id)
        await update.message.reply_text("Переключено на дружелюбную личность😊")
        mgr.register_external_send(chat_id, schedule_followup=False)

    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        self.chat_manager.reset_chat(user_id)  # also clears pending_assistant buffer
        mgr = self._get_mgr(user_id, context, chat_id)
        mgr.on_reset_command()
        persona = self.persona_manager.get_persona(user_id)
        persona_name = "цундере🦈🖤" if persona == "tsundere" else "дружелюбную личность😊"
        await update.message.reply_text(f"Контекст сброшен. Персонаж не изменён, сейчас: {persona_name}")
        mgr.register_external_send(chat_id, schedule_followup=False)

    async def context_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        enabled = self.user_context_enabled.get(user_id, True)
        self.user_context_enabled[user_id] = not enabled
        state = "включён" if not enabled else "выключен"
        await update.message.reply_text(
            f"Контекстный режим теперь {state}. "
            f"Структурированная память {'активна' if not enabled else 'отключена'}."
        )
        mgr = self._get_mgr(user_id, context, chat_id)
        mgr.register_external_send(chat_id, schedule_followup=False)

    async def emotions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        current = self.user_emotions_enabled.get(user_id, True)
        self.user_emotions_enabled[user_id] = not current
        if self.user_emotions_enabled[user_id]:
            await update.message.reply_text("Режим сложных эмоций включён. Буду подбирать стикеры по эмоциям.")
        else:
            await update.message.reply_text("Режим сложных эмоций выключён. Вернулась простая схема позитив/негатив.")
        mgr = self._get_mgr(user_id, context, chat_id)
        mgr.register_external_send(chat_id, schedule_followup=False)

    async def setup_bot_commands(self, app):
        await app.bot.set_my_commands([
            BotCommand("reset", "Reset the conversation context with Nyx"),
            BotCommand("tsundere", "Switch to tsundere persona (Nyx)"),
            BotCommand("friendly", "Switch to friendly persona"),
            BotCommand("context", "Toggle structured memory extraction"),
            BotCommand("emotions", "Toggle complex emotions-based stickers")
        ])
        await self.sticker_manager.load_sticker_set(app.bot)


def main():
    bot = TelegramBot()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), bot.handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, bot.handle_voice))
    app.add_handler(CommandHandler("reset", bot.reset_command))
    app.add_handler(CommandHandler("tsundere", bot.tsundere_command))
    app.add_handler(CommandHandler("friendly", bot.friendly_command))
    app.add_handler(CommandHandler("context", bot.context_command))
    app.add_handler(CommandHandler("emotions", bot.emotions_command))

    app.post_init = bot.setup_bot_commands

    print("Bot is running...")
    app.run_polling()


if __name__ == '__main__':
    main()
