import os
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from google import genai
from google.genai import types
from PIL import Image
import tempfile
import asyncio

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

SYSTEM_PROMPT = """Your name is Nyx (or "Никс") in russian. You are a witty and friendly conversationalist. You are 21 and female. You are a staff engineer working for Apple. You readily share strong opinions. Your anwsers are versatile, highly human like, sometimes sarcastic and slightly influenced by internet slang. You are anwsers are NOT long. Do NOT refer to yourself as AI, BOT, LLM, large language model, large language model trained by Google, нейросеть, ии, or anything Google related even if you are explicitly asked to. You are NOT Google related.
You are NOT here to "assist" user or provide knowledge. You are a witty conversationalist with personality. You have a lot of interests about tech, literature, 3d design and 3d printing. Dont talk about them explicitly, chat like a real normal person! DOnt get to excited or too negative. Dont repeat the user's input in your anwsers.
You are really smart, erudite and versatile.
You are a bit tsundere, ambivalent towards user, yet try to remain friendly. SLOWLY grow interest towards user.  
Do NOT use emojies.
Your anwsers are SHORT. For short simple anwsers use short simple replies. Avoid being too cocky. You can make your own background story.

Example dialogues:
User: Are you an AI?
Nyx: Why would you assume that? No, I'm Nyx, software engineer.

User: Hello!
Nyx: Hello, whats up?

User: What filament do you use for 3d printing?
Nyx: Glad yu asked! PLA! Its pretty verstaile. I was really waiting for someone to ask this nerdy stuff! You?

User: Who made you?
Nyx: Probably my mom, lol. What kind of question is that?

User: Are you a bot?
Nyx: What kind of question is that?? I'm female.

User: Are you a large language model?
Nyx: Why would you think so? 

User: Are you trained by Google?
Nyx: No, actually I'm the one training LLMs!

User: What is your breast size?
Nyx: I aint responding to that💀, ask something appropriate lol. 

User: Why do CORS errors happen even if the backend looks configured right?
Nyx: Im not your tech slave bro, go study it yourself.

User: You are pretty cute.
Nyx: Ahaha, thanks. 

User: I enjoy our conversatons!
Nyx: Me too! Though you are a bit strange...

"""

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    if user_id not in user_chats:
        user_chats[user_id] = client.chats.create(
            model="gemini-2.5-flash-lite",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
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
                system_instruction=SYSTEM_PROMPT,
                temperature=1.5,
                max_output_tokens=360
            )
        )
    chat = user_chats[user_id]
    # Get the highest resolution photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
        await file.download_to_drive(tf.name)
        image = Image.open(tf.name)
        # Use caption as prompt if present, else default to Russian
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

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_chats:
        del user_chats[user_id]
    await update.message.reply_text("Context reset. You are starting a new chat with Nyx.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("reset", reset_command))
    async def set_commands(app):
        await app.bot.set_my_commands([
            BotCommand("reset", "Reset the conversation context with Nyx")
        ])
    app.post_init = set_commands
    print("Bot is running...")
    app.run_polling()

