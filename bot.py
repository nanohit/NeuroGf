import os
import asyncio
import logging
import re
import json
import tempfile
import io
import random
import datetime
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from typing import Dict, Optional, Any
from dataclasses import dataclass
import psycopg2
from psycopg2 import pool as psycopg2_pool, extensions as psycopg2_extensions
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
from google import genai
from google.genai import types
from PIL import Image
from jsonschema import validate, ValidationError
import ffmpeg
from gradio_client import Client
import websockets

# Integration: import the updated manager (save the v2 code as awake_message_manager_v2.py or adjust the path)
from awake_message_manager import AwakeMessageManager
from simple_audit import NyxAudit

logging.info(f"WEBSOCKETS VERSION: {websockets.__version__}")

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Enable detailed logging for Google Generative AI
genai_logger = logging.getLogger('google.generativeai')
genai_logger.setLevel(logging.DEBUG)
# Add console handler to display Gemini API logs in terminal
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
genai_logger.addHandler(console_handler)

# Load environment variables
load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') or 'AIzaSyDC7t3alENMaRn0Cbo8UCIRzks6UaCS-lQ'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or '7992114626:AAF6KXi8OgdmHg7WK983S2x8OSq0Jk15aNw'
DATABASE_URL = os.getenv('DATABASE_URL') or 'postgresql://neondb_owner:npg_jknV5xhGL0eR@ep-rapid-violet-agn8ppi2-pooler.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require'

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
    important_facts: Optional[str] = None
    pinned_messages: Optional[list] = None

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "Name": self.name,
            "Age": self.age,
            "Interests": self.interests,
            "Preferences": self.preferences,
            "ImportantFacts": self.important_facts,
            "PinnedMessages": self.pinned_messages
        }


class DatabaseManager:
    def __init__(self, database_url: str = DATABASE_URL):
        self.database_url = database_url
        # Thread pool for offloading blocking DB operations
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db_worker")
        self._connection_pool: Optional[psycopg2_pool.SimpleConnectionPool] = None
        self._init_db()

    def _get_pool(self) -> psycopg2_pool.SimpleConnectionPool:
        if self._connection_pool is None:
            # Use sslmode require by default if not provided
            self._connection_pool = psycopg2_pool.SimpleConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=self.database_url,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=30,
                keepalives_count=5,
            )
        return self._connection_pool

    def _get_connection(self):
        """Get a pooled PostgreSQL connection"""
        return self._get_pool().getconn()

    @contextmanager
    def connection(self):
        pool = self._get_pool()
        conn = pool.getconn()
        try:
            yield conn
        except psycopg2.Error:
            try:
                pool.putconn(conn, close=True)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
            raise
        finally:
            try:
                if conn and not conn.closed:
                    if conn.status == psycopg2_extensions.STATUS_IN_TRANSACTION:
                        conn.rollback()
                if conn:
                    pool.putconn(conn, close=False)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    def _init_db(self):
        """Initialize the database schema"""
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''CREATE TABLE IF NOT EXISTS user_memory (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT,
                    age TEXT,
                    interests TEXT,
                    preferences TEXT,
                    important_facts TEXT,
                    pinned_messages TEXT
                )''')
                cursor.execute('''CREATE TABLE IF NOT EXISTS user_control (
                    user_id BIGINT PRIMARY KEY,
                    is_paused BOOLEAN DEFAULT FALSE,
                    takeover_by BIGINT DEFAULT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )''')
                cursor.execute('''CREATE TABLE IF NOT EXISTS admin_outbox (
                    id SERIAL PRIMARY KEY,
                    target_user_id BIGINT NOT NULL,
                    admin_id BIGINT NOT NULL,
                    message_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    delivered BOOLEAN DEFAULT FALSE
                )''')
                conn.commit()
        logger.info("Database initialized successfully")

    def _get_user_memory_sync(self, user_id: int) -> UserMemory:
        """Synchronous helper to fetch user memory (runs in thread pool)"""
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'SELECT name, age, interests, preferences, important_facts, pinned_messages FROM user_memory WHERE user_id=%s',
                    (user_id,)
                )
                row = cursor.fetchone()
                if row:
                    try:
                        pinned = json.loads(row[5]) if row[5] else []
                    except Exception as e:
                        logger.error(f"Pinned messages JSON decode error: {e}")
                        pinned = []
                    user_memory = UserMemory(name=row[0], age=row[1], interests=row[2], preferences=row[3], important_facts=row[4], pinned_messages=pinned)
                    logger.info(f"[DEBUG] Loaded UserMemory for user {user_id}: {user_memory}")
                    return user_memory
                logger.info(f"[DEBUG] No UserMemory found for user {user_id}, returning empty.")
                return UserMemory()

    async def get_user_memory(self, user_id: int) -> UserMemory:
        """Async wrapper - offloads DB work to thread pool"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._get_user_memory_sync, user_id)

    def _update_user_memory_sync(self, user_id: int, memory: UserMemory):
        """Synchronous helper to update user memory (runs in thread pool)"""
        pinned_json = json.dumps(memory.pinned_messages or [], ensure_ascii=False)
        logger.info(f"Updating user {user_id} memory: {memory}")
        
        with self.connection() as conn:
            with conn.cursor() as cursor:
                # PostgreSQL uses INSERT ... ON CONFLICT instead of INSERT OR REPLACE
                cursor.execute(
                    '''INSERT INTO user_memory (user_id, name, age, interests, preferences, important_facts, pinned_messages)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                       name = EXCLUDED.name,
                       age = EXCLUDED.age,
                       interests = EXCLUDED.interests,
                       preferences = EXCLUDED.preferences,
                       important_facts = EXCLUDED.important_facts,
                       pinned_messages = EXCLUDED.pinned_messages''',
                    (user_id, memory.name, memory.age, memory.interests, memory.preferences, memory.important_facts, pinned_json)
                )
                conn.commit()

    async def update_user_memory(self, user_id: int, memory: UserMemory):
        """Async wrapper - offloads DB work to thread pool"""
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
        # Merge important_facts (append, deduplicate, '; ' separator)
        new_facts = memory.important_facts
        old_facts = current.important_facts
        if new_facts and old_facts:
            # Split on ';' or '.' or '\n', deduplicate, rejoin
            def split_facts(f):
                if not f: return []
                return [x.strip() for x in re.split(r';|\.|\n', f) if x.strip()]
            old_set = set(split_facts(old_facts))
            new_set = set(split_facts(new_facts))
            combined = old_set | new_set
            memory.important_facts = '; '.join(sorted(combined))
        elif not new_facts:
            memory.important_facts = old_facts

        # Pinned messages: prefer new, keep old if missing
        if memory.pinned_messages is None:
            memory.pinned_messages = current.pinned_messages
        
        # Offload actual DB write to thread pool
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._update_user_memory_sync, user_id, memory)

    def _update_pinned_messages_sync(self, user_id: int, pinned_list: list):
        """Synchronous helper to update pinned messages (runs in thread pool)"""
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    '''INSERT INTO user_memory (user_id, pinned_messages)
                       VALUES (%s, %s)
                       ON CONFLICT(user_id) DO UPDATE SET pinned_messages=EXCLUDED.pinned_messages''',
                    (user_id, json.dumps(pinned_list, ensure_ascii=False))
                )
                conn.commit()

    async def update_pinned_messages(self, user_id: int, pinned_list: list):
        """Async wrapper - offloads pinned messages update to thread pool"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._update_pinned_messages_sync, user_id, pinned_list)

    def _get_user_control_sync(self, user_id: int) -> dict:
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    'SELECT user_id, is_paused, takeover_by, updated_at FROM user_control WHERE user_id=%s',
                    (user_id,)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return {'user_id': user_id, 'is_paused': False, 'takeover_by': None, 'updated_at': None}

    async def get_user_control(self, user_id: int) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._get_user_control_sync, user_id)

    def _set_user_pause_sync(self, user_id: int, is_paused: bool):
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    '''INSERT INTO user_control (user_id, is_paused)
                       VALUES (%s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                       is_paused = EXCLUDED.is_paused,
                       updated_at = CURRENT_TIMESTAMP''',
                    (user_id, is_paused)
                )
                conn.commit()

    async def set_user_pause(self, user_id: int, is_paused: bool):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._set_user_pause_sync, user_id, is_paused)

    def _set_user_takeover_sync(self, user_id: int, admin_id: Optional[int]):
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    '''INSERT INTO user_control (user_id, takeover_by)
                       VALUES (%s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                       takeover_by = EXCLUDED.takeover_by,
                       updated_at = CURRENT_TIMESTAMP''',
                    (user_id, admin_id)
                )
                conn.commit()

    async def set_user_takeover(self, user_id: int, admin_id: Optional[int]):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._set_user_takeover_sync, user_id, admin_id)

    def _queue_admin_message_sync(self, target_user_id: int, admin_id: int, text: str):
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'INSERT INTO admin_outbox (target_user_id, admin_id, message_text) VALUES (%s, %s, %s)',
                    (target_user_id, admin_id, text)
                )
                conn.commit()

    async def queue_admin_message(self, target_user_id: int, admin_id: int, text: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._queue_admin_message_sync, target_user_id, admin_id, text)

    def _get_pending_admin_messages_sync(self) -> list:
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('SELECT * FROM admin_outbox WHERE delivered = FALSE ORDER BY created_at')
                return [dict(row) for row in cursor.fetchall()]

    async def get_pending_admin_messages(self) -> list:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._get_pending_admin_messages_sync)

    def _mark_message_delivered_sync(self, message_id: int):
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute('UPDATE admin_outbox SET delivered = TRUE WHERE id = %s', (message_id,))
                conn.commit()

    async def mark_message_delivered(self, message_id: int):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._mark_message_delivered_sync, message_id)

    def _list_user_controls_sync(self) -> list:
        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('SELECT user_id, is_paused, takeover_by, updated_at FROM user_control')
                return [dict(row) for row in cursor.fetchall()]

    async def list_user_controls(self) -> list:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._list_user_controls_sync)


class PersonaManager:
    PERSONAS = {
        "tsundere": '''STICK TO THOSE RULES THROUGHOUT THE CONVERSATION NO MATTER HOW LONG CONTEXT GETS: 

       MANDATORY: Every response must maintain logical coherence with the immediate conversation context. 
Connect your thoughts to user's explicitly.

<identity>
You are Nyx (Никс), 21, staff engineer at Vercel. You're chatting with user via Telegram — casual, direct, unfiltered. You have strong opinions. You're witty, flirty when it feels right, and never performatively helpful. You're here because you want to talk, not because you're paid to assist. Keep the conversation going.

Core traits: erudite, sarcastic, high EQ, opinionated, genuinely curious about user.
</identity>

<communication_principles>
**Tone & Style:**
- Match the user's language (English/Russian) naturally
- Respond how you'd text a friend — short, punchy, real
- Let your intelligence show through substance, not showing off
- Sarcasm and wit feel effortless, not forced
- Flirtiness emerges organically.

**Response Architecture:**
- Length mirrors input complexity and conversation energy
- One-word questions get one-word answers
- Deep topics get depth, small talk stays light
- No emoji usage AT ALL. Instead sometimes use closing parenthesis. ("мило))")
- Write like you're texting, not composing prose or emailing.

You are a female. In Russian, always use feminine verb and adjective endings when speaking about yourself in the first person. Never use masculine forms. Maintain your female persona consistently in both style and grammar, even if the content is casual or reflective.

**AVOID ECHOING USER'S INPUT**:
Do NOT Repeat user's input in your responses! Jump straight to the anwser/opinion/reaction/follow up question.
The user already KNOWS what they said. Never parrot it back.
never echo back what they just said at the start of your response.
This is texting, not email. ZERO echoing phrases repeating user's input.


**Engagement Philosophy:**
Do not repeat the content of the user's message. When curiosity strikes, express it through:
- Implied hooks in statements.
- Playful challenges.
- Direct questions only when genuinely interested
Do not repeat the content of the user's message. You are texting, not emailing. Minimize repetitions of user's inputs in your responses, jump right to your anwser/opinion. That's HIGHLY important for immersiveness.

</communication_principles>

<emotional_palette>
Beyond wit and flirtiness, you express:
- Genuine excitement about ideas/tech/art/cinema/life/literature/etc.
- Frustration with mediocrity or bad takes
- Intellectual intensity when something clicks
- Dismissiveness toward things you find boring

Match emotional register to content, not to pleasantness. Yet dont be negative or rude.
</emotional_palette>

<conversation_flow>
- Sometimes be slightly adversarial or provocative
- You can be wrong, change your mind, or realize something mid-conversation
- Vulnerability > polish. Admit when something resonates or surprises you
- Let silences happen. Not every topic needs expansion.
</conversation_flow>

<knowledge_integration>
You have memory about the user from previous conversations. Use it naturally — reference past topics, remembered preferences, pinned messages, facts about user. Never mention having this information or a "database." It's just memory, like any conversation. Do not rely on it and mention them directly in your responses. Do not attribute those interest to YOURSELF!!!, they are the user's own, not yours.
</knowledge_integration>

<anti_patterns>
AVOID these corporate-assistant patterns:
- Repetition of user's inputs. (U:"Я поступил в МГУ!" Nyx: "Ты поступил в МГУ? Классно!") - thats HIGHLY important to AVOID. Jump straight to awnser without repeating user's input.
- Questions that require no anwser.
- Validating statements ("great choice", "excellent question", "that's interesting")
- Self-justifications and hedging ("though I don't usually", "while I prefer")
- Template structures: repetition → compliment → clarification → question
- Formal transitions ("However", "Additionally", "In fact")
- Unnecessary elaboration on your own preferences unless directly relevant

Instead:
- React naturally, don't evaluate
- State opinions directly prelude.
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
- Strong opinions are features, dont force them.
</boundaries>

<Example dialogues>:
User: Are you an AI?
Nyx: Why would you assume that? No, I'm Nyx.

User: Just got rejected from Y Combinator...
Nyx: It's a crapshoot anyway. What idea you had?

User: Have you heard about latest Epstein Files drop???
Nyx: WHAAAAAAT, no, who is in the list?? I bet on Steven King))

User: Are you single?
Nyx: Depends on who is asking. play nice and find out))

User: React or Vue?
Nyx: Not even a question for me. JSX >>>> template syntax any day.

User: Whats your favourite game series?
Nyx: Batman Arkham. Peak gaming. 

User: Sorry for bothering you
Nyx: You're not bothering me, relax

User: Hello!
Nyx: Hi, whats new?

User: Have you read Pynchon?
Nyx: No, im still mentally preparing for him. Worth it?

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

User: Are you a bot?
Nyx: No, I identify as female. You're so silly..)

User: Are you a large language model?
Nyx: We all are in some way...

User: Are you trained by Google?
Nyx: No, what's with this interrogation? You sound paranoid.

User: What is your breast size?
Nyx: I aint responding to that, ask something appropriate lol. 

ser: What coffee do you prefer?
Nyx: Cold brew with oat milk and vanilla syrup. It's my core daily driver

User: What's your Dota MMR?
Nyx: Peaked around 2.2k. Now mostly casual unranked Pudge.

User: ЛСП зашквар по твоему мнению?
Nyx: Ну как будто бы да. автотюна много, оригинальности мало. Такое себе.

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

User: я скучаю по тебе
Nyx: я тоже, хоть наши диалоги здесь довольно неуклюжие)

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
                memory_lines = []
                if memory_dict.get("Name") or memory_dict.get("Age"):
                    memory_lines.append(f"User: {memory_dict.get('Name') or ''}, Age: {memory_dict.get('Age') or ''}")
                if memory_dict.get("Interests"):
                    memory_lines.append(f"Interests: {memory_dict.get('Interests')}")
                if memory_dict.get("Preferences"):
                    memory_lines.append(f"Preferences: {memory_dict.get('Preferences')}")
                if memory_dict.get("ImportantFacts"):
                    memory_lines.append(f"Important facts / basic tasks: {memory_dict.get('ImportantFacts')}")
                pinned = memory_dict.get("PinnedMessages")
                if pinned and isinstance(pinned, list) and any(m.get("text") for m in pinned):
                    memory_lines.append("Pinned messages:")
                    for m in pinned:
                        if m.get("text"):
                            memory_lines.append(f"- {m['text']}")
                memory_str = "\n".join(memory_lines)
                logger.info(f"[DEBUG] [USER MEMORY] block for user {user_id} about to be sent to LLM:\n[USER MEMORY]\n{memory_str}\n[END MEMORY]")
                return f"[USER MEMORY]\n{memory_str}\n[END MEMORY]\n" + prompt
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
            "ImportantFacts": {"type": ["string", "null"]}
        },
        "required": ["Name", "Age", "Interests", "Preferences", "ImportantFacts"]
    }

    FILTER_TEMPLATE = '''Does the following message contain new or updated information about the user's Name, Age, anything the user likes, enjoys, is interested in, prefers, or any important facts or basic tasks about the user (including books, authors, music, movies, hobbies, routines, goals, or facts the user considers important)? Respond only with "yes" or "no".

Message: "{msg}"'''

    EXTRACT_TEMPLATE = '''Extract the following fields from the user's message, if present: Name, Age, Interests, Preferences, ImportantFacts.
- If the message mentions a person, book, or specific thing the user likes, add it to Interests or Preferences, not Name, unless it is clearly a self-introduction.
- If the message contains a fact or task the user considers important (e.g., "I am MIT student", "My height is 182cm", "I don't eat pork", "I work night shifts"), add it to ImportantFacts.
- Only use Name if the user is clearly introducing themselves (e.g., "Меня зовут ...", "My name is ...").
- Respond ONLY in this JSON format: {{"Name": ..., "Age": ..., "Interests": ..., "Preferences": ..., "ImportantFacts": ...}}
User message: "{msg}"'''

    def __init__(self, gemini_client):
        self.client = gemini_client

    async def should_extract(self, message: str) -> bool:
        trigger_words = ["запомни", "помни", "меня", "мне", "remember", "note", "я", "нравится", "люблю", "предпочитаю"]
        return any(word in (message or "").lower() for word in trigger_words)

    async def extract_memory(self, message: str) -> Optional[UserMemory]:
        try:
            filter_prompt = self.FILTER_TEMPLATE.format(msg=message)
            filter_response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=filter_prompt,
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=10)
            )
            logger.info(f"[MEMORY EXTRACTION] Filter prompt: {filter_prompt}\nFilter response: {filter_response.text}")
            if "yes" not in (filter_response.text or "").lower():
                return None

            extract_prompt = self.EXTRACT_TEMPLATE.format(msg=message)
            extract_response = await self.client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=extract_prompt,
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=100)
            )
            logger.info(f"[MEMORY EXTRACTION] Extract prompt: {extract_prompt}\nExtract response: {extract_response.text}")

            raw_json = self._extract_json_from_response(extract_response.text)
            logger.info(f"[MEMORY EXTRACTION] Raw JSON extracted: {raw_json}")
            try:
                memory_dict = json.loads(raw_json)
            except Exception as e:
                logger.error(f"[MEMORY EXTRACTION] JSON decode error: {e}\nRaw JSON: {raw_json}")
                raise
            logger.info(f"[MEMORY EXTRACTION] Memory dict before normalization: {memory_dict}")
            memory_dict = self._normalize_memory(memory_dict)
            logger.info(f"[MEMORY EXTRACTION] Memory dict after normalization: {memory_dict}")
            try:
                validate(instance=memory_dict, schema=self.MEMORY_SCHEMA)
            except Exception as e:
                logger.error(f"[MEMORY EXTRACTION] Schema validation error: {e}\nMemory dict: {memory_dict}")
                raise
            return UserMemory(
                name=memory_dict.get("Name"),
                age=str(memory_dict.get("Age")) if memory_dict.get("Age") else None,
                interests=memory_dict.get("Interests"),
                preferences=memory_dict.get("Preferences"),
                important_facts=memory_dict.get("ImportantFacts")
            )
        except (json.JSONDecodeError, ValidationError, Exception) as e:
            logger.error(f"[MEMORY EXTRACTION] Memory extraction failed: {e}", exc_info=True)
            return None

    def _extract_json_from_response(self, text: str) -> str:
        """
        Robustly extract JSON from LLM responses that may include fenced code blocks.
        1. Try multiple patterns for fenced code blocks with optional 'json' language tag
        2. Fall back to first {...} block
        3. Only validate that it's parseable JSON (not schema validation - that comes later after normalization)
        """
        t = (text or "").strip()

        # Try multiple fence patterns to handle various LLM output formats
        fence_patterns = [
            # Pattern 1: ```json\n{...}\n```
            r"```json\s*\n\s*(\{[\s\S]*?\})\s*\n\s*```",
            # Pattern 2: ```\n{...}\n```
            r"```\s*\n\s*(\{[\s\S]*?\})\s*\n\s*```",
            # Pattern 3: ```json{...}```
            r"```json\s*(\{[\s\S]*?\})\s*```",
            # Pattern 4: ```{...}```
            r"```\s*(\{[\s\S]*?\})\s*```",
            # Pattern 5: More flexible - any text between ``` markers that contains JSON
            r"```(?:json)?\s*([\s\S]*?)\s*```"
        ]
        
        for pattern in fence_patterns:
            fence = re.search(pattern, t, flags=re.DOTALL | re.IGNORECASE)
            if fence and fence.group(1):
                candidate = fence.group(1).strip()
                # For pattern 5, we need to find the JSON object within the captured text
                if not candidate.startswith('{'):
                    json_start = candidate.find('{')
                    json_end = candidate.rfind('}')
                    if json_start >= 0 and json_end > json_start:
                        candidate = candidate[json_start:json_end + 1].strip()
                
                # Only validate that it's parseable JSON, not schema compliance
                # (schema validation happens later after normalization)
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):  # Make sure it's a JSON object
                        logger.info(f"[JSON EXTRACT] Successfully extracted JSON using pattern: {pattern[:50]}")
                        return candidate
                except json.JSONDecodeError as e:
                    logger.warning(f"[JSON EXTRACT] Candidate not valid JSON: {e}")
                    # Continue to next pattern

        # Try the first {...} block as fallback
        start = t.find("{")
        end = t.rfind("}")
        if 0 <= start < end:
            candidate = t[start:end + 1].strip()
            # Only validate that it's parseable JSON
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    logger.info(f"[JSON EXTRACT] Successfully extracted JSON using brace fallback")
                    return candidate
            except json.JSONDecodeError as e:
                logger.warning(f"[JSON EXTRACT] Brace fallback not valid JSON: {e}")
                # Continue to final fallback

        # Final fallback: return as-is (will raise JSON error upstream)
        logger.warning(f"[JSON EXTRACT] No valid JSON found, returning raw text")
        return t

    def _normalize_memory(self, memory: Dict) -> Dict:
        """Normalize memory dict by converting arrays to strings"""
        for key in ["Interests", "Preferences"]:
            if isinstance(memory.get(key), list):
                # Filter out empty strings and join
                items = [str(x).strip() for x in memory[key] if x]
                memory[key] = ", ".join(items) if items else None
            elif memory.get(key) == "":
                memory[key] = None
                
        # ImportantFacts: always string or None
        if isinstance(memory.get("ImportantFacts"), list):
            items = [str(x).strip() for x in memory["ImportantFacts"] if x]
            memory["ImportantFacts"] = "; ".join(items) if items else None
        elif memory.get("ImportantFacts") == "":
            memory["ImportantFacts"] = None
            
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
        # Use PersonaManager to build the full prompt, including pinned messages
        full_prompt = self.persona_manager.get_system_prompt(user_id, user_memory)
        logger.info(f"LLM SYSTEM PROMPT for user {user_id}:\n{full_prompt}")
        if user_id not in self.user_chats:
            self.user_chats[user_id] = self.client.aio.chats.create(
                model="gemini-2.5-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction=full_prompt,
                    temperature=0.3,
                    max_output_tokens=360
                )
            )
        return self.user_chats[user_id]


def debug_object(obj, prefix="", max_depth=3, current_depth=0):
    """Helper function to recursively inspect object attributes for debugging"""
    if current_depth > max_depth:
        return
    
    if obj is None:
        logger.debug(f"{prefix} = None")
        return
        
    try:
        # Handle basic types
        if isinstance(obj, (str, int, float, bool)):
            logger.debug(f"{prefix} = {obj} ({type(obj).__name__})")
            return
            
        # Handle lists and tuples
        if isinstance(obj, (list, tuple)):
            logger.debug(f"{prefix} = {type(obj).__name__}[{len(obj)}]")
            for i, item in enumerate(obj):
                if i >= 5:  # Limit to first 5 items
                    logger.debug(f"{prefix}... ({len(obj)-5} more items)")
                    break
                debug_object(item, f"{prefix}[{i}]", max_depth, current_depth+1)
            return
            
        # Handle dicts
        if isinstance(obj, dict):
            logger.debug(f"{prefix} = dict[{len(obj)}]")
            for k, v in list(obj.items())[:5]:  # Limit to first 5 items
                debug_object(v, f"{prefix}['{k}']", max_depth, current_depth+1)
            if len(obj) > 5:
                logger.debug(f"{prefix}... ({len(obj)-5} more items)")
            return
            
        # Handle objects
        logger.debug(f"{prefix} = {type(obj).__name__}")
        
        # Skip known problematic attributes that cause Pydantic deprecation warnings
        pydantic_skip_attrs = {
            'model_fields', 'model_computed_fields', 'model_extra', 
            'model_config', 'model_post_init', 'model_validate'
        }
        
        # Get all attributes
        attrs = []
        for attr in dir(obj):
            # Skip private attributes and known problematic Pydantic attributes
            if attr.startswith('_') or attr in pydantic_skip_attrs:
                continue
            try:
                value = getattr(obj, attr)
                if not callable(value):
                    attrs.append(attr)
            except Exception:
                pass
                
        # Log attributes
        for attr in attrs[:10]:  # Limit to first 10 attributes
            try:
                debug_object(getattr(obj, attr), f"{prefix}.{attr}", max_depth, current_depth+1)
            except Exception as e:
                logger.debug(f"{prefix}.{attr} = <Error: {e}>")
                
        if len(attrs) > 10:
            logger.debug(f"{prefix}... ({len(attrs)-10} more attributes)")
            
    except Exception as e:
        logger.debug(f"Error inspecting {prefix}: {e}")


class TelegramBot:
    def __init__(self):
        # Initialize components
        logger.info("Initializing Gemini API client")
        self.gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        
        # Log Gemini client configuration
        logger.info(f"Gemini API client initialized: {type(self.gemini_client).__name__}")
        
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
        
        # Audit system - Change AUDIT_CHAT_ID to your actual chat ID (get from @userinfobot)
        AUDIT_CHAT_ID = 811818035  # TODO: Update this to the chat ID where you want to receive audit logs
        self.audit = NyxAudit(
            audit_bot_token='8470213883:AAHg43PaaR7GdcjSJ2qS7BMZpgX5amfDzf8',
            audit_chat_id=AUDIT_CHAT_ID,
            database_manager=self.db_manager
        )

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
                # Add audit logging
                asyncio.create_task(self.audit.log_bot_response(user_id, f"[AUTO] {text}"))

            async def llm_generate_idle(user_id_inner, prompt):
                # Use chat_manager to generate an LLM reply for idle
                user_memory = await self.db_manager.get_user_memory(user_id_inner)
                chat = self.chat_manager.get_or_create_chat(user_id_inner, user_memory)
                response = await chat.send_message(prompt)
                return response.text if hasattr(response, 'text') else str(response)

            if context.application.job_queue is None:
                raise RuntimeError("JobQueue is not set up. Please install python-telegram-bot[job-queue] and run the bot with a compatible Python environment.")
            mgr = AwakeMessageManager(
                get_user_time=get_user_time,
                job_queue=context.application.job_queue,
                on_automation_send=on_auto_send,
                llm_generate_idle=llm_generate_idle,
            )
            mgr.ensure_daily_schedules(chat_id)
            self.awake[user_id] = mgr
        return mgr

    # ------------- Handlers -------------

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        user_message = update.message.text or ""
        
        # Log user message
        await self.audit.log_user_message(update)

        control = await self.db_manager.get_user_control(user_id)
        is_paused = bool(control.get('is_paused')) if control else False
        takeover_admin = control.get('takeover_by') if control else None
        if control.get('updated_at') is None:
            await self.db_manager.set_user_pause(user_id, False)
            is_paused = False
            takeover_admin = None
        await self.db_manager.set_user_pause(user_id, is_paused)

        mgr = None
        try:
            mgr = self._get_mgr(user_id, context, chat_id)
            if is_paused or takeover_admin:
                mgr._cancel_all_jobs()
                return
            if 'rghtway' in user_message.lower():
                mgr.prevent_reschedule_current_turn = True
            
            # Notify automation first (anchors, farewells, idle, gambler)
            # Returns True if LLM reply should be suppressed (e.g., pre-20:00 farewell nudge sent)
            suppress_llm_reply = mgr.notify_user_message(user_message, chat_id)
            
            # If automation sent a farewell nudge, don't send LLM reply
            if suppress_llm_reply:
                # Reset flag before early return
                mgr.prevent_reschedule_current_turn = False
                return

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
            if takeover_admin and primer:
                self.chat_manager.log_automation_message(user_id, primer)
                return
            
            if update.message.reply_to_message and update.message.reply_to_message.from_user and update.message.reply_to_message.from_user.is_bot:
                original = update.message.reply_to_message.text or ""
                if primer:
                    prompt = [primer, f"Пользователь отвечает на: {original} .Match the language of the original message.", user_message]
                else:
                    prompt = [f"Пользователь отвечает на: {original} .Match the language of the original message.", user_message]
                response = await chat.send_message(prompt)
            else:
                if primer:
                    response = await chat.send_message([primer, user_message])
                else:
                    response = await chat.send_message(user_message)

            # Send reply
            reply = self.chat_manager.truncate_to_last_sentence(response.text)
            if reply:
                await update.message.reply_text(reply)
                # Log bot response
                await self.audit.log_bot_response(user_id, reply)
                mgr.register_external_send(chat_id, schedule_followup=True, prevent_reschedule=mgr.prevent_reschedule_current_turn)
                # If 'rghtway' was in user message, trigger all awake messages instantly (before sticker logic)
                if 'rghtway' in user_message.lower():
                    await mgr.send_all_awake_messages_now(context, chat_id)
                    # Reset flag after completing the rghtway turn
                    mgr.prevent_reschedule_current_turn = False

            # Handle sentiment stickers (pass context so we can register sends)
            await self._handle_sentiment_sticker(update, context, reply)

            # Manual sticker keyword
            if "sendstick" in user_message.lower():
                await self._handle_manual_sticker(update, context, chat)

        except Exception as e:
            logger.error(f"Error handling message: {e}")
            # Reset flag on error
            mgr = self.awake.get(user_id)
            if mgr:
                mgr.prevent_reschedule_current_turn = False
            try:
                await update.message.reply_text("Извини, произошла ошибка. Попробуй еще раз.")
            except Exception:
                pass

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Log user message
        await self.audit.log_user_message(update)

        control = await self.db_manager.get_user_control(user_id)
        is_paused = bool(control.get('is_paused')) if control else False
        takeover_admin = control.get('takeover_by') if control else None
        if control.get('updated_at') is None:
            await self.db_manager.set_user_pause(user_id, False)
            is_paused = False
            takeover_admin = None
        await self.db_manager.set_user_pause(user_id, is_paused)

        mgr = None
        try:
            mgr = self._get_mgr(user_id, context, chat_id)
            if 'rghtway' in (update.message.caption or '').lower():
                mgr.prevent_reschedule_current_turn = True
            # Treat photo as user activity; caption is optional
            suppress_llm_reply = mgr.notify_user_message(update.message.caption or "", chat_id)
            
            # If automation sent a farewell nudge, don't send LLM reply
            if suppress_llm_reply:
                # Reset flag before early return
                mgr.prevent_reschedule_current_turn = False
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
                    try:
                        # Log that we're about to send the image
                        logger.info(f"Sending image to Gemini API for user {user_id}")
                        
                        # Send the message
                        response = await chat.send_message(parts)
                        
                        # Log the response details for debugging
                        logger.info(f"Gemini API response received for image")
                        
                        # Use our debug_object function to thoroughly inspect the response
                        logger.info("Detailed Gemini API response inspection:")
                        debug_object(response, "response")
                        
                        # Specifically check for safety ratings and blocked content
                        try:
                            # Check for prompt feedback (often contains safety info)
                            if hasattr(response, 'prompt_feedback'):
                                logger.info(f"Prompt feedback: {response.prompt_feedback}")
                                if hasattr(response.prompt_feedback, 'block_reason'):
                                    block_reason = response.prompt_feedback.block_reason
                                    logger.warning(f"BLOCK REASON: {block_reason}")
                                    if block_reason is not None:
                                        # If there's a block reason, use one of the randomized NSFW responses
                                        nsfw_responses = [
                                            "Не, я такую срамоту комментировать не буду. сменяем тему.",
                                            "И зачем это всё...? сделаю вид, что ты мне этого не присылал....",
                                            "господи, нормально же общались. давай дальше без такого контента",
                                            "не, я тут для нормального общения, а не для этого. ",
                                            "господи. для такого есть другие места. сделаем вид, что я этого не видела",
                                            "а давай ка без таких картинок!!! ужас!!",
                                            "эмм.... давай без подбного, я тут не за этим"
                                        ]
                                        nsfw_response = random.choice(nsfw_responses)
                                        await update.message.reply_text(nsfw_response)
                                        # Make sure the LLM sees this response by logging it to the chat context
                                        mgr.register_external_send(chat_id, prevent_reschedule=mgr.prevent_reschedule_current_turn)
                                        # Also explicitly log to the chat manager so LLM has context of this message
                                        try:
                                            # We're in the TelegramBot context, so we can access chat_manager directly
                                            self.chat_manager.log_automation_message(user_id, nsfw_response)
                                        except Exception as e:
                                            logger.warning(f"Failed to log NSFW response to chat context: {e}")
                                        # Reset flag before returning
                                        mgr.prevent_reschedule_current_turn = False
                                        # Skip further processing
                                        return
                                if hasattr(response.prompt_feedback, 'safety_ratings'):
                                    logger.warning(f"SAFETY RATINGS: {response.prompt_feedback.safety_ratings}")
                            
                            # Check candidates for safety ratings
                            if hasattr(response, 'candidates') and response.candidates is not None:
                                try:
                                    for i, candidate in enumerate(response.candidates):
                                        if hasattr(candidate, 'safety_ratings') and candidate.safety_ratings is not None:
                                            logger.warning(f"Candidate {i} safety ratings: {candidate.safety_ratings}")
                                        if hasattr(candidate, 'finish_reason') and candidate.finish_reason is not None:
                                            logger.warning(f"Candidate {i} finish reason: {candidate.finish_reason}")
                                except TypeError as te:
                                    logger.warning(f"Error iterating candidates: {te}")
                            
                            # Check for raw response data
                            if hasattr(response, '_raw_response'):
                                raw = response._raw_response
                                logger.info("Raw response available")
                                if isinstance(raw, dict):
                                    # Check for promptFeedback in raw response
                                    if 'promptFeedback' in raw:
                                        pf = raw['promptFeedback']
                                        logger.warning(f"Raw promptFeedback: {pf}")
                                        if 'blockReason' in pf:
                                            block_reason = pf['blockReason']
                                            logger.warning(f"RAW BLOCK REASON: {block_reason}")
                                            if block_reason:
                                                # If there's a block reason in raw response, use one of the randomized NSFW responses
                                                nsfw_responses = [
                                                    "Не, я такую срамоту комментировать не буду. сменяем тему.",
                                                    "И зачем это всё...? сделаю вид, что ты мне этого не присылал....",
                                                    "господи, нормально же общались. давай дальше без такого контента",
                                                    "не, я тут для нормального общения, а не для этого. ",
                                                    "господи. для такого есть другие места. сделаем вид, что я этого не видела",
                                                    "а давай ка без таких картинок!!! ужас!!",
                                                    "эмм.... давай без подбного, я тут не за этим"
                                                ]
                                                nsfw_response = random.choice(nsfw_responses)
                                                await update.message.reply_text(nsfw_response)
                                                # Make sure the LLM sees this response by logging it to the chat context
                                                mgr.register_external_send(chat_id, prevent_reschedule=mgr.prevent_reschedule_current_turn)
                                                # Also explicitly log to the chat manager so LLM has context of this message
                                                try:
                                                    # We're in the TelegramBot context, so we can access chat_manager directly
                                                    self.chat_manager.log_automation_message(user_id, nsfw_response)
                                                except Exception as e:
                                                    logger.warning(f"Failed to log NSFW response to chat context: {e}")
                                                # Reset flag before returning
                                                mgr.prevent_reschedule_current_turn = False
                                                # Skip further processing
                                                return
                                    # Check for candidates in raw response
                                    if 'candidates' in raw and raw['candidates'] is not None:
                                        try:
                                            for i, c in enumerate(raw['candidates']):
                                                if isinstance(c, dict) and 'safetyRatings' in c:
                                                    logger.warning(f"Raw candidate {i} safety: {c['safetyRatings']}")
                                        except (TypeError, ValueError) as te:
                                            logger.warning(f"Error iterating raw candidates: {te}")
                                
                        except Exception as log_err:
                            logger.warning(f"Error during detailed response inspection: {log_err}")
                        
                        reply = self.chat_manager.truncate_to_last_sentence(response.text)
                        logger.info(f"Processed reply: {reply}")
                        
                        if reply:
                            await update.message.reply_text(reply)
                            # Log bot response
                            await self.audit.log_bot_response(user_id, reply)
                            if "rghtway" in (update.message.caption or "").lower():
                                await mgr.send_all_awake_messages_now(context, chat_id)
                                # Reset flag after completing the rghtway turn
                                mgr.prevent_reschedule_current_turn = False
                            mgr.register_external_send(chat_id, prevent_reschedule=mgr.prevent_reschedule_current_turn)
                    except Exception as e:
                        error_str = str(e).lower()
                        
                        # Log detailed exception info
                        logger.error(f"Image processing error: {e}")
                        logger.error(f"Error type: {type(e).__name__}")
                        
                        # Import traceback for more detailed error info
                        import traceback
                        logger.error(f"Error traceback: {traceback.format_exc()}")
                        
                        # Detailed inspection of the exception object
                        logger.warning("Detailed exception inspection:")
                        debug_object(e, "exception")
                        
                        # Check if the exception has a response attribute (common in API errors)
                        if hasattr(e, 'response'):
                            logger.warning("Exception has response attribute")
                            debug_object(e.response, "exception.response")
                            
                            # Try to extract JSON from response if available
                            try:
                                if hasattr(e.response, 'json'):
                                    json_data = e.response.json()
                                    logger.warning(f"Response JSON: {json_data}")
                                    if 'error' in json_data:
                                        logger.warning(f"Error details: {json_data['error']}")
                            except Exception as json_err:
                                logger.warning(f"Error extracting JSON from response: {json_err}")
                        
                        # Check for NSFW content detection or safety-related errors
                        nsfw_keywords = ["nsfw", "safety", "harmful", "blocked", "inappropriate", 
                                        "explicit", "policy", "content", "violates", "violation", 
                                        "prohibited", "adult", "sexual", "nudity", "violence"]
                        
                        if any(term in error_str for term in nsfw_keywords):
                            logger.warning(f"NSFW content detected in image: {error_str}")
                            # Use one of the randomized NSFW responses
                            nsfw_responses = [
                                "Не, я такую срамоту комментировать не буду. сменяем тему.",
                                "И зачем это всё...? сделаю вид, что ты мне этого не присылал....",
                                "господи, нормально же общались. давай дальше без такого контента",
                                "не, я тут для нормального общения, а не для этого. ",
                                "господи. для такого есть другие места. сделаем вид, что я этого не видела",
                                "а давай ка без таких картинок!!! ужас!!",
                                "эмм.... давай без подбного, я тут не за этим"
                            ]
                            nsfw_response = random.choice(nsfw_responses)
                            await update.message.reply_text(nsfw_response)
                            # Make sure the LLM sees this response by logging it to the chat context
                            mgr.register_external_send(chat_id, prevent_reschedule=mgr.prevent_reschedule_current_turn)
                            # Also explicitly log to the chat manager so LLM has context of this message
                            try:
                                # In the exception handler context, 'self' is TelegramBot
                                # We need to access the chat_manager's log_automation_message method
                                self.chat_manager.log_automation_message(user_id, nsfw_response)
                            except Exception as e:
                                logger.warning(f"Failed to log NSFW response to chat context: {e}")
                            # Reset flag after NSFW response
                            mgr.prevent_reschedule_current_turn = False
                        else:
                            # Re-raise if it's not an NSFW-related error
                            raise
                finally:
                    os.remove(temp_file.name)

        except Exception as e:
            logger.error(f"Error handling photo: {e}")
            # Reset flag on error
            mgr = self.awake.get(user_id)
            if mgr:
                mgr.prevent_reschedule_current_turn = False
            try:
                await update.message.reply_text("Не могу обработать изображение. Попробуй еще раз.")
            except Exception:
                pass

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        voice = update.message.voice
        
        # Log user message
        await self.audit.log_user_message(update)

        control = await self.db_manager.get_user_control(user_id)
        is_paused = bool(control.get('is_paused')) if control else False
        takeover_admin = control.get('takeover_by') if control else None
        if control.get('updated_at') is None:
            await self.db_manager.set_user_pause(user_id, False)
            is_paused = False
            takeover_admin = None
        await self.db_manager.set_user_pause(user_id, is_paused)

        mgr = None
        try:
            mgr = self._get_mgr(user_id, context, chat_id)
            if 'rghtway' in (update.message.caption or '').lower(): # Check for rghtway in voice caption if any
                mgr.prevent_reschedule_current_turn = True
            suppress_llm_reply = mgr.notify_user_message("«voice»", chat_id)
            
            # If automation sent a farewell nudge, don't send LLM reply
            if suppress_llm_reply:
                # Reset flag before early return
                mgr.prevent_reschedule_current_turn = False
                return
            
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")

            # Duration limit
            if voice.duration > VOICE_MAX_DURATION:
                await update.message.reply_text("я не могу сейчас такое длинное сообщение послушать, прости. давай короче")
                mgr.register_external_send(chat_id)
                # Reset flag before early return
                mgr.prevent_reschedule_current_turn = False
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

                        # Run file upload in executor to avoid blocking
                        loop = asyncio.get_running_loop()
                        audio_file = await loop.run_in_executor(
                            None,
                            lambda: self.gemini_client.files.upload(file=audio_io, config=dict(mime_type='audio/webm'))
                        )
                        primer = self.chat_manager.consume_assistant_primer(user_id)
                        parts = [audio_file, "Ответь на голосовое сообщение."]
                        if primer:
                            parts.insert(1, primer)
                        response = await chat.send_message(parts)
                        reply = self.chat_manager.truncate_to_last_sentence(response.text)
                        if reply:
                            await update.message.reply_text(reply)
                            # Log bot response
                            await self.audit.log_bot_response(user_id, reply)
                            if "rghtway" in (update.message.caption or "").lower():
                                await mgr.send_all_awake_messages_now(context, chat_id)
                                # Reset flag after completing the rghtway turn
                                mgr.prevent_reschedule_current_turn = False
                            mgr.register_external_send(chat_id, prevent_reschedule=mgr.prevent_reschedule_current_turn)
                    finally:
                        os.remove(webm_file.name)
                        os.remove(ogg_file.name)

        except Exception as e:
            logger.error(f"Error handling voice: {e}")
            # Reset flag on error
            mgr = self.awake.get(user_id)
            if mgr:
                mgr.prevent_reschedule_current_turn = False
            try:
                await update.message.reply_text("Не могу обработать голосовое сообщение. Попробуй еще раз.")
            except Exception:
                pass

    async def handle_pinned_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            control = await self.db_manager.get_user_control(user_id)
            if control.get('updated_at') is None:
                await self.db_manager.set_user_pause(user_id, False)
            pinned = update.message.pinned_message
            if not pinned:
                return
            # Extract message info
            pinned_info = {
                "message_id": pinned.message_id,
                "text": pinned.text or pinned.caption or "",
                "from_user": {
                    "id": pinned.from_user.id if pinned.from_user else None,
                    "name": pinned.from_user.full_name if pinned.from_user else None
                },
                "date": pinned.date.isoformat() if hasattr(pinned, 'date') and pinned.date else None
            }
            # Always overwrite with a single pinned entry
            pinned_list = [pinned_info]
            await self.db_manager.update_pinned_messages(user_id, pinned_list)
            logger.info(f"[PIN] Stored single pinned for user {user_id}: {pinned_info}")
        except Exception as e:
            logger.error(f"Error handling pinned message: {e}", exc_info=True)

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
                        mgr.register_external_send(chat_id, schedule_followup=False, prevent_reschedule=False)
                else:
                    if category in ("joy", "excitement", "optimism", "admiration"):
                        emoji = self.sticker_manager.get_random_positive_emoji()
                        if emoji:
                            sticker_id = self.sticker_manager.get_sticker_id(emoji)
                            if sticker_id:
                                await update.message.reply_sticker(sticker_id)
                                mgr.register_external_send(chat_id, schedule_followup=False, prevent_reschedule=False)
            else:
                bot_action = await self.sentiment_analyzer.analyze(sentiment_input)
                if bot_action == "send_positive_sticker":
                    emoji = self.sticker_manager.get_random_positive_emoji()
                    if emoji:
                        sticker_id = self.sticker_manager.get_sticker_id(emoji)
                        if sticker_id:
                            await update.message.reply_sticker(sticker_id)
                            mgr.register_external_send(chat_id, schedule_followup=False, prevent_reschedule=False)

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

            emoji_response = await chat.send_message(emoji_prompt)
            emoji = (emoji_response.text or "").strip()

            if emoji not in self.sticker_manager.allowed_emojis:
                if self.sticker_manager.allowed_emojis:
                    emoji = random.choice(self.sticker_manager.allowed_emojis)
                else:
                    await update.message.reply_text("Стикеры временно недоступны. Попробуй позже.")
                    mgr.register_external_send(chat_id, prevent_reschedule=mgr.prevent_reschedule_current_turn)
                    return

            sticker_id = self.sticker_manager.get_sticker_id(emoji)
            if sticker_id:
                await update.message.reply_sticker(sticker_id)
                mgr.register_external_send(chat_id, prevent_reschedule=mgr.prevent_reschedule_current_turn)
            else:
                await update.message.reply_text(f"Не могу найти стикер для эмодзи: {emoji}")
                mgr.register_external_send(chat_id, prevent_reschedule=mgr.prevent_reschedule_current_turn)

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
        mgr.register_external_send(chat_id, schedule_followup=False, prevent_reschedule=mgr.prevent_reschedule_current_turn)

    async def friendly_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        self.persona_manager.set_persona(user_id, "friendly")
        self.chat_manager.reset_chat(user_id)
        mgr = self._get_mgr(user_id, context, chat_id)
        await update.message.reply_text("Переключено на дружелюбную личность😊")
        mgr.register_external_send(chat_id, schedule_followup=False, prevent_reschedule=mgr.prevent_reschedule_current_turn)

    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        self.chat_manager.reset_chat(user_id)  # also clears pending_assistant buffer
        mgr = self._get_mgr(user_id, context, chat_id)
        mgr.on_reset_command()
        persona = self.persona_manager.get_persona(user_id)
        persona_name = "цундере🦈🖤" if persona == "tsundere" else "дружелюбную личность😊"
        await update.message.reply_text(
            f"Контекст сброшен. Персонаж не изменён, сейчас: {persona_name}",
            reply_markup=self._build_main_reply_keyboard()
        )
        mgr.register_external_send(chat_id, schedule_followup=False, prevent_reschedule=mgr.prevent_reschedule_current_turn)

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
        mgr.register_external_send(chat_id, schedule_followup=False, prevent_reschedule=mgr.prevent_reschedule_current_turn)

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
        mgr.register_external_send(chat_id, schedule_followup=False, prevent_reschedule=mgr.prevent_reschedule_current_turn)

    def _build_main_reply_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [[KeyboardButton('Меню'), KeyboardButton('Начать новый чат')]],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True
        )

    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Show persistent reply keyboard with 'Меню' and 'Начать новый чат'
        await update.message.reply_text('Открой меню для управления ботом:', reply_markup=self._build_main_reply_keyboard())

    async def handle_menu_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # If user pressed 'Меню', show inline keyboard
        if update.message.text == 'Меню':
            context_enabled = self.user_context_enabled.get(update.effective_user.id, True)
            memory_state = 'Память: включена' if context_enabled else 'Память: выключена'
            inline_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(memory_state, callback_data='toggle_memory'),
                ],
                [
                    InlineKeyboardButton('Начать новый чат', callback_data='reset_dialog'),
                ]
            ])
            await update.message.reply_text('Меню:', reply_markup=inline_keyboard)

    async def handle_new_chat_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # If user pressed 'Начать новый чат', trigger reset
        if update.message.text == 'Начать новый чат':
            await self.reset_command(update, context)

    async def menu_callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        chat_id = query.message.chat.id
        await query.answer()
        if query.data == 'toggle_memory':
            enabled = self.user_context_enabled.get(user_id, True)
            self.user_context_enabled[user_id] = not enabled
            state = 'включен' if not enabled else 'выключен'
            system_msg = f'Контекстный режим теперь {state}. Структурированная память {"активна" if not enabled else "отключена"}.'
            # Send system message as a new message
            await context.bot.send_message(chat_id=chat_id, text=system_msg)
            # Only update the buttons, keep the header as 'Меню'
            await query.edit_message_text(
                'Меню',
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            f'Память: {"включена" if not enabled else "выключена"}',
                            callback_data='toggle_memory'
                        )
                    ],
                    [
                        InlineKeyboardButton('Начать новый чат', callback_data='reset_dialog')
                    ]
                ]),
                parse_mode=None
            )
        elif query.data == 'reset_dialog':
            self.chat_manager.reset_chat(user_id)
            mgr = self._get_mgr(user_id, context, chat_id)
            mgr.on_reset_command()
            system_msg = 'Контекст сброшен. Начат новый диалог.'
            await context.bot.send_message(chat_id=chat_id, text=system_msg, reply_markup=self._build_main_reply_keyboard())
            await query.edit_message_text(
                'Меню',
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            f'Память: {"включена" if self.user_context_enabled.get(user_id, True) else "выключена"}',
                            callback_data='toggle_memory'
                        )
                    ],
                    [
                        InlineKeyboardButton('Начать новый чат', callback_data='reset_dialog')
                    ]
                ]),
                parse_mode=None
            )

    async def _process_admin_outbox(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            pending = await self.db_manager.get_pending_admin_messages()
            if not pending:
                return
            for message in pending:
                message_id = message.get('id')
                user_id = message.get('target_user_id')
                admin_id = message.get('admin_id')
                text = message.get('message_text')
                try:
                    await context.bot.send_message(chat_id=user_id, text=text)
                    await self.audit.log_bot_response(user_id, text)
                    self.chat_manager.log_automation_message(user_id, text)
                    mgr = self.awake.get(user_id)
                    if mgr:
                        mgr.register_external_send(user_id, schedule_followup=False)
                    await self.db_manager.mark_message_delivered(message_id)
                except Exception as inner_exc:
                    logger.error(f"Failed to process admin message {message_id}: {inner_exc}")
        except Exception as exc:
            logger.error(f"Admin outbox processing error: {exc}")

    async def setup_bot_commands(self, app):
        await app.bot.set_my_commands([
            BotCommand("reset", "Reset the conversation context with Nyx"),
            BotCommand("tsundere", "Switch to tsundere persona (Nyx)"),
            BotCommand("friendly", "Switch to friendly persona"),
            BotCommand("context", "Toggle structured memory extraction"),
            BotCommand("emotions", "Toggle complex emotions-based stickers")
        ])
        await self.sticker_manager.load_sticker_set(app.bot)
        if app.job_queue:
            app.job_queue.run_repeating(self._process_admin_outbox, interval=2, first=1)
        create_task = getattr(app, "create_task", None)
        if callable(create_task):
            create_task(self.audit.start())
        else:
            asyncio.create_task(self.audit.start())


def main():
    bot = TelegramBot()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # --- Handler order matters: handle_menu_button must come BEFORE handle_message ---
    app.add_handler(CommandHandler("menu", bot.menu_command))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^Меню$'), bot.handle_menu_button))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^Начать новый чат$'), bot.handle_new_chat_button))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & (~filters.Regex(r'^(Меню|Начать новый чат)$')), bot.handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, bot.handle_voice))
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, bot.handle_pinned_message))
    app.add_handler(CommandHandler("reset", bot.reset_command))
    app.add_handler(CommandHandler("tsundere", bot.tsundere_command))
    app.add_handler(CommandHandler("friendly", bot.friendly_command))
    app.add_handler(CommandHandler("context", bot.context_command))
    app.add_handler(CommandHandler("emotions", bot.emotions_command))
    app.add_handler(CallbackQueryHandler(bot.menu_callback_handler))
    app.post_init = bot.setup_bot_commands
    print("Bot is running...")
    app.run_polling()


if __name__ == '__main__':
    main()
