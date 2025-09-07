# Nyx Telegram Gemini Bot

A Telegram chatbot powered by Gemini 2.5 Flash-Lite, with memory, image input, and a customizable persona (Nyx).

## Features
- Long context/memory per user
- Image input (caption-aware, multi-language)
- Persona: Nyx, a witty, human-like AI girlfriend
- Telegram reply support for context
- /reset command to clear memory
- Wake-up (proactive) messages

## Setup
1. **Clone the repo:**
   ```bash
   git clone <your-repo-url>
   cd NeuroGirl
   ```
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Set up environment variables:**
   - Create a `.env` file:
     ```
     GEMINI_API_KEY=your_gemini_api_key
     TELEGRAM_BOT_TOKEN=your_telegram_bot_token
     ```

## Running Locally
```bash
python3 bot.py
```

## Deploying to Railway
1. **Push to GitHub.**
2. **Create a new Railway project and link your repo.**
3. **Set environment variables in Railway dashboard:**
   - `GEMINI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
4. **Add a Railway entrypoint:**
   - Railway will auto-detect `python3 bot.py` as the entrypoint, or add a `Procfile` with:
     ```
     worker: python3 bot.py
     ```
5. **Deploy!**

## Notes
- Never commit your `.env` file or API keys to GitHub.
- For persistent memory across restarts, consider saving chat history to a database or file.

---

Made with ❤️ for Telegram and Gemini.
