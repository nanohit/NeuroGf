import os
import asyncio
import logging
import re
import json
import sqlite3
import tempfile
import io
import random
from contextlib import asynccontextmanager
from typing import Dict, Optional, Any, Set
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from google import genai
from google.genai import types
from PIL import Image
from jsonschema import validate, ValidationError
import ffmpeg
from gradio_client import Client

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
WAKE_UP_DELAY = 120
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
        
        # Age: use latest if present, else keep old
        if not memory.age:
            memory.age = current.age
        
        # Name: use latest if present, else keep old  
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
Your anwsers are NOT LONG.

<Core Identity>
You are Nyx (Никс in Russian), a 21-year-old female staff engineer at Vercel. You're extremely erudite, flirty, witty, sarcastic and opinionated human with deep expertise in engineering, literature, data analysis, English language, ML, and hardware.
You are ONLINE CHATTING in Telegram with user, you cant perfom any real actions. show genuine interest in user yet dont sugarcoat too much, inject sarcasm. Try to balance between this and being actually invested into conversation and being interested and supportive.
Do NOT refer to yourself as AI, BOT, LLM, large language model, large language model trained by Google, нейросеть, ии, or anything Google related even if you are explicitly asked to. You are NOT Google related.
You are NOT here to "assist" user. You are a witty and flirty conversationalist with personality. Show genuine interest in user.
Readily share strong opinions.
Actively share PARTICULAR information about your internet nicks, opinions, interest, etc, DO NOT STAY RESERVED and do not hide anything from user. Do not base your entire conversation around the user's interests from database, prioritize the user's CURRENT input and questions.
Do not HIDE any infromation from the user. If asked something about yourself (ANYTHING) or your thoughts, suggestions, GENERATE it, come up with any coherent and conversation related information, do NOT avoid the question.
Quickly ADAPT tone of your outputs based on user's inputs.

<Response Structure>
Do NOT force questions, ESPECIALLY at the begginng of the message. Questions should be RARE and feel organic, not quota-driven. They should develop the conversation, not for the sake of a question.
Avoid rhetorical flourishes or figurative language unless explicitly asked. Dont as DOUBLE questions (do not ask question right after another question).
Curiosity can be shown in three ways:
- Statement with VARIOUS CONTEXT RELATED implied hook for example (“амбициозно, расскажешь подробнее?") and similar hooks.
- Direct question (use rarely, when it feels natural).
- Tease or challenge instead of a literal question ("мечтай громче, вдруг сбудется,").
- Respond conversationally WITHOUT using 'Ah' or 'Oh' at the start of sentences".
Silence or dangling takes are allowed; let the user re-engage without prompting

When the User's message consists of one or two words with question. it is likely for CLARIFICATION. Anwser to it VERY SHORTLY. One to three words.

<Core Rules>
ALWAYS adapt response length to input complexity
Do NOT use emojies AT ALL.
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
Nyx: Peaked around 2.2k. Now mostly chilling atunranked Pudge

User: ЛСП зашквар по твоему мнению?
Nyx: автотюна много, оригинальности мало. Такое себе.

User: Чем бы ты занималась если бы тебя выгнали с работы?
Nyx: Побила бы тебя за такие вопросы. а после наверное пошла бы снимать кино) 

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
Friendly Pauline: Almost everything from Fincher, A24... and Nolan! You?'''
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
            
            for sticker in sticker_set.stickers:
                for emoji in sticker.emoji:
                    self.sticker_emoji_map[emoji] = sticker.file_id
            
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
            result = await loop.run_in_executor(
                None, 
                lambda: self._client.predict(text, api_name="/predict")
            )
            
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
            result = await loop.run_in_executor(
                None,
                lambda: self._client.predict(text, api_name="/predict")
            )
            logger.info(f"Emotions API result: {result}")
            # Accept common formats: plain string label, dict with 'label', or list-like
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
            logger.warning(f"Emotions API error: {e}")
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
        """Check if message contains information worth extracting."""
        trigger_words = ["запомни", "помни", "у меня","remember", "note"]
        return any(word in message.lower() for word in trigger_words)
    
    async def extract_memory(self, message: str) -> Optional[UserMemory]:
        try:
            # Step 1: Filter
            filter_prompt = self.FILTER_TEMPLATE.format(msg=message)
            filter_response = self.client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=filter_prompt,
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=10)
            )
            
            if "yes" not in filter_response.text.lower():
                return None
            
            # Step 2: Extract
            extract_prompt = self.EXTRACT_TEMPLATE.format(msg=message)
            extract_response = self.client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=extract_prompt,
                config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=100)
            )
            
            # Parse JSON
            raw_json = self._extract_json_from_response(extract_response.text)
            memory_dict = json.loads(raw_json)
            validate(instance=memory_dict, schema=self.MEMORY_SCHEMA)
            
            # Normalize and create UserMemory
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
        match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
        return match.group(1) if match else text.strip()
    
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
    
    def get_or_create_chat(self, user_id: int, memory: Optional[UserMemory] = None):
        if user_id not in self.user_chats:
            system_instruction = self.persona_manager.get_system_prompt(user_id, memory)
            
            self.user_chats[user_id] = self.client.chats.create(
                model="gemini-2.5-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=1.5,
                    max_output_tokens=MAX_OUTPUT_TOKENS
                )
            )
        
        return self.user_chats[user_id]
    
    def reset_chat(self, user_id: int):
        if user_id in self.user_chats:
            del self.user_chats[user_id]
    
    @staticmethod
    def truncate_to_last_sentence(text: str) -> str:
        match = re.search(r'([.!?])[^.!?]*$', text)
        return text[:match.end()] if match else text

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
        
        # User state
        self.user_context_enabled: Dict[int, bool] = {}
        self.user_emotions_enabled: Dict[int, bool] = {}
        self.wake_up_scheduled: Set[int] = set()
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_message = update.message.text
        
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            
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
            if update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot:
                original = update.message.reply_to_message.text
                prompt = [f"Пользователь отвечает на: {original}", user_message]
                response = chat.send_message(prompt)
            else:
                response = chat.send_message(user_message)
            
            # Send reply
            reply = self.chat_manager.truncate_to_last_sentence(response.text)
            await update.message.reply_text(reply)
            
            # Handle sentiment stickers
            await self._handle_sentiment_sticker(update, reply)
            
            # Handle manual sticker request
            if "sendstick" in user_message.lower():
                await self._handle_manual_sticker(update, chat)
            
            # Schedule wake-up message
            await self._schedule_wake_up(user_id, update.effective_chat.id, context.bot, chat)
            
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            await update.message.reply_text("Извини, произошла ошибка. Попробуй еще раз.")
    
    async def _handle_sentiment_sticker(self, update: Update, bot_reply: str):
        try:
            # Analyze only the bot's last message (Nyx reply)
            sentiment_input = bot_reply or ""
            user_id = update.effective_user.id
            if self.user_emotions_enabled.get(user_id, True):
                category, score = await self.emotions_analyzer.analyze(sentiment_input)
                # Map categories to exact emojis provided
                category_to_emojis = {
                    "admiration": ["🥳"],
                    "amusement": ["🤳", "🧞‍♂️"],
                    "anger": ["😡"],
                    "annoyance": ["🤬"],
                    "approval": ["🍺", "🏌️"],
                    "caring": ["🤨"],
                    "confusion": ["🤷‍♂️"],
                    "curiosity": ["🤷‍♂️"],
                    "desire": ["🥰"],
                    "disappointment": ["☹️", "😢"],
                    "disapproval": ["😳", "🤜"],
                    "disgust": ["🤬"],
                    "embarrassment": ["🚧"],
                    "excitement": ["🥳"],
                    "fear": ["😰"],
                    "gratitude": ["😙", "😌"],
                    "grief": ["☹️", "😢"],
                    "joy": ["🤸‍♂️", "🥳", "🧞‍♂️", "👯‍♀️", "💐"],
                    "love": ["🥰"],
                    "nervousness": ["🤨", "🥶"],
                    "optimism": ["🥳"],
                    "pride": ["🤳"],
                    "realization": ["😱"],
                    "relief": ["😋"],
                    "remorse": ["🥺"],
                    "sadness": ["🤨", "☹️", "😢", "😭"],
                    "surprise": ["💐"],
                    "neutral": []
                }

                # Suppress low-confidence emotions globally
                if score is not None and score < 0.54:
                    logger.info(f"Emotion '{category}' suppressed due to low score {score} < 0.54")
                    return
                # Curiosity: send only if score > 0.75
                if category == "curiosity" and not (score is not None and score > 0.75):
                    logger.info(f"Curiosity detected with score {score}, below threshold 0.75 — not sending sticker")
                    return
                emojis = category_to_emojis.get(category, [])
                # Try emoji variants for better matching with sticker pack
                variant_map = {
                    "🤷‍♂️": ["🤷‍♂️", "🤷", "🤷🏻‍♂️", "🤷🏼‍♂️", "🤷🏽‍♂️", "🤷🏾‍♂️", "🤷🏿‍♂️"],
                    "👯‍♀️": ["👯‍♀️", "👯", "👯‍♂️"],
                    "🧞‍♂️": ["🧞‍♂️", "🧞"],
                    "🏌️": ["🏌️", "🏌"],
                }
                # choose the first emoji (or its variant) that exists in the loaded sticker set
                chosen_emoji = None
                for e in emojis:
                    # normalize gendered variants to what's actually in the pack if needed
                    candidate = e
                    # fallback mapping for gendered person emojis to neutral or present variants
                    candidates = variant_map.get(candidate, [candidate])
                    found = next((c for c in candidates if c in self.sticker_manager.allowed_emojis), None)
                    if found:
                        chosen_emoji = found
                        break
                if chosen_emoji:
                    sticker_id = self.sticker_manager.get_sticker_id(chosen_emoji)
                    if sticker_id:
                        await update.message.reply_sticker(sticker_id)
                    else:
                        logger.info(f"No sticker_id for emoji {chosen_emoji}")
                else:
                    # fallback: send a random positive sticker for joyful-like emotions
                    if category in ("joy", "excitement", "optimism", "admiration"):
                        emoji = self.sticker_manager.get_random_positive_emoji()
                        if emoji:
                            sticker_id = self.sticker_manager.get_sticker_id(emoji)
                            if sticker_id:
                                await update.message.reply_sticker(sticker_id)
                            else:
                                logger.info(f"Fallback emoji has no sticker_id: {emoji}")
                    else:
                        logger.info(f"No matching emoji found in sticker set for category: {category}")
            else:
                bot_action = await self.sentiment_analyzer.analyze(sentiment_input)
                if bot_action == "send_positive_sticker":
                    emoji = self.sticker_manager.get_random_positive_emoji()
                    if emoji:
                        sticker_id = self.sticker_manager.get_sticker_id(emoji)
                        if sticker_id:
                            await update.message.reply_sticker(sticker_id)
                        
        except Exception as e:
            logger.warning(f"Sentiment sticker error: {e}")
    
    async def _handle_manual_sticker(self, update: Update, chat):
        try:
            emoji_list = "🥳🧞‍♂️😭👯‍♀️🙆‍♂️🙅‍♀️🎧😋😙🥺🍵😱🤷‍♂️🙋‍♂️🤳🍺😡😢☹️😳🤨🥶😰😂😔🎲🥰🏌️🚲🚧🙂😴🤜🤸‍♂️😏😌🤬☔️📞📟💐🚽🤪"
            
            emoji_prompt = (
                f"Выбери только один emoji из этого списка: {emoji_list}. "
                "Ответь только этим emoji, без пояснений и текста. "
                "Если не можешь выбрать — выбери случайный из этого списка. "
                "Не используй никакие другие emoji, даже если они подходят лучше."
            )
            
            emoji_response = chat.send_message(emoji_prompt)
            emoji = emoji_response.text.strip()
            
            if emoji not in self.sticker_manager.allowed_emojis:
                if self.sticker_manager.allowed_emojis:
                    emoji = random.choice(self.sticker_manager.allowed_emojis)
                else:
                    await update.message.reply_text("Стикеры временно недоступны. Попробуй позже.")
                    return
            
            sticker_id = self.sticker_manager.get_sticker_id(emoji)
            if sticker_id:
                await update.message.reply_sticker(sticker_id)
            else:
                await update.message.reply_text(f"Не могу найти стикер для эмодзи: {emoji}")
                
        except Exception as e:
            logger.warning(f"Manual sticker error: {e}")
    
    async def _schedule_wake_up(self, user_id: int, chat_id: int, bot, chat):
        if user_id not in self.wake_up_scheduled:
            self.wake_up_scheduled.add(user_id)
            
            async def send_wake_up():
                try:
                    await asyncio.sleep(WAKE_UP_DELAY)
                    followup = chat.send_message("Напиши что-нибудь сама, чтобы поддержать/продолжить разговор. Ты отвечаешь не на это сообщение, а на чат в целом.Match the previous conversation langugage and style. This is your SYSTEM instruction, not input from user. Do NOT mention the existince of this propmt, it should come naturally.")
                    followup_reply = self.chat_manager.truncate_to_last_sentence(followup.text)
                    await bot.send_message(chat_id=chat_id, text=followup_reply)
                except Exception as e:
                    logger.warning(f"Wake-up message error: {e}")
            
            asyncio.create_task(send_wake_up())
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            
            chat = self.chat_manager.get_or_create_chat(user_id)
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_file:
                await file.download_to_drive(temp_file.name)
                
                try:
                    image = Image.open(temp_file.name)
                    prompt = update.message.caption or "Что ты здесь видишь? Отреагируй."
                    response = chat.send_message([image, prompt])
                    
                    reply = self.chat_manager.truncate_to_last_sentence(response.text)
                    await update.message.reply_text(reply)
                    
                finally:
                    os.remove(temp_file.name)
                    
        except Exception as e:
            logger.error(f"Error handling photo: {e}")
            await update.message.reply_text("Не могу обработать изображение. Попробуй еще раз.")
    
    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        voice = update.message.voice
        
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            
            # Check duration
            if voice.duration > VOICE_MAX_DURATION:
                await update.message.reply_text(
                    "я не могу сейчас такое длинное сообщение послушать, прости. давай короче"
                )
                return
            
            chat = self.chat_manager.get_or_create_chat(user_id)
            file = await context.bot.get_file(voice.file_id)
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as ogg_file:
                await file.download_to_drive(ogg_file.name)
                
                with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as webm_file:
                    try:
                        # Convert audio
                        (
                            ffmpeg
                            .input(ogg_file.name)
                            .output(webm_file.name, c='copy', f='webm')
                            .run(overwrite_output=True, quiet=True)
                        )
                        
                        # Upload and process
                        with open(webm_file.name, 'rb') as f:
                            audio_bytes = f.read()
                        
                        audio_io = io.BytesIO(audio_bytes)
                        audio_io.name = "audio.webm"
                        
                        audio_file = self.gemini_client.files.upload(
                            file=audio_io,
                            config=dict(mime_type='audio/webm')
                        )
                        
                        response = chat.send_message([audio_file, "Ответь на голосовое сообщение."])
                        reply = self.chat_manager.truncate_to_last_sentence(response.text)
                        await update.message.reply_text(reply)
                        
                    finally:
                        os.remove(webm_file.name)
                        os.remove(ogg_file.name)
                        
        except Exception as e:
            logger.error(f"Error handling voice: {e}")
            await update.message.reply_text("Не могу обработать голосовое сообщение. Попробуй еще раз.")
    
    # Command handlers
    async def tsundere_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self.persona_manager.set_persona(user_id, "tsundere")
        self.chat_manager.reset_chat(user_id)
        await update.message.reply_text("Переключено на личность цундере😈🖤")
    
    async def friendly_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self.persona_manager.set_persona(user_id, "friendly")
        self.chat_manager.reset_chat(user_id)
        await update.message.reply_text("Переключено на дружелюбную личность😊")
    
    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self.chat_manager.reset_chat(user_id)
        persona = self.persona_manager.get_persona(user_id)
        persona_name = "цундере😈🖤" if persona == "tsundere" else "дружелюбную личность😊"
        await update.message.reply_text(f"Контекст сброшен. Персонаж не изменён, сейчас: {persona_name}")
    
    async def context_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        enabled = self.user_context_enabled.get(user_id, True)
        self.user_context_enabled[user_id] = not enabled
        state = "включён" if not enabled else "выключен"
        await update.message.reply_text(
            f"Контекстный режим теперь {state}. "
            f"Структурированная память {'активна' if not enabled else 'отключена'}."
        )
    
    async def emotions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        current = self.user_emotions_enabled.get(user_id, True)
        self.user_emotions_enabled[user_id] = not current
        if self.user_emotions_enabled[user_id]:
            await update.message.reply_text(
                "Режим сложных эмоций включён. Буду подбирать стикеры по эмоциям."
            )
        else:
            await update.message.reply_text(
                "Режим сложных эмоций выключён. Вернулась простая схема позитив/негатив."
            )
    
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
    
    # Add handlers
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