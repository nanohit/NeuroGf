# awake_message_manager_v2.py

from typing import Callable, Optional, Dict
import datetime
import random
import re
from telegram.ext import JobQueue, CallbackContext, Job
from apscheduler.jobstores.base import JobLookupError
import logging

from telegram_retry import send_message_with_retry

class AwakeMessageManager:
    """
    Orchestrates automated outreach per user: morning/evening pings (time-attached),
    gambler follow-ups, idle nudges, active hours, daily quota (automation only),
    and good-night shutdown. Designed to coexist with an LLM reply path:
    - No automatic immediate reply.
    - Exposes notify_user_message() to start chains and handle farewells.
    - Exposes register_external_send() to align idle/guards with bot.py sends.
    - Exposes is_sending_allowed_now() for bot.py to honor hours/shutdown.
    """

    MORNING_PROB = 0.65
    MORNING_HELLO_PROB = 0.65
    EVENING_PROB = 0.65

    GAMBLE_ATTEMPT_PROBS = [0.50, 0.40, 0.25, 0.25, 0.25]  # max 5/day
    GAMBLE_DELAY_MIN_SEC = 3 * 60
    GAMBLE_DELAY_MAX_SEC = 35 * 60

    FOLLOWUP_FIRST_DELAY_MIN_SEC = 3 * 60
    FOLLOWUP_FIRST_DELAY_MAX_SEC = 35 * 60

    IDLE_DELAY_MIN_SEC = 45 * 60
    IDLE_DELAY_MAX_SEC = 3 * 60 * 60

    ACTIVE_START_HOUR = 8
    ACTIVE_END_HOUR = 23  # exclusive

    DAILY_QUOTA = 8  # applies to automation managed here

    def __init__(self, get_user_time: Callable[[], datetime.datetime], job_queue: JobQueue, on_automation_send: Optional[Callable[[int, str], None]] = None, llm_generate_idle: Optional[Callable[[int, str], str]] = None):
        self.get_user_time = get_user_time
        self.job_queue = job_queue
        self.on_automation_send = on_automation_send
        self.llm_generate_idle = llm_generate_idle

        # Set up logger
        self.logger = logging.getLogger("AwakeMessageManager")
        if not self.logger.hasHandlers():
            handler = logging.StreamHandler()
            formatter = logging.Formatter('[%(asctime)s] %(levelname)s:%(name)s: %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

        # Day-scoped state
        self.current_day: datetime.date = self.get_user_time().date()
        self.quota_remaining: int = self.DAILY_QUOTA

        self.morning_sent_today: bool = False
        self.evening_sent_today: bool = False

        # Gambler attempts consumed today (send or skip still consumes)
        self.gamble_attempts_today: int = 0

        # Activity timestamps
        self.last_user_message_time: Optional[datetime.datetime] = None
        self.last_bot_message_time: Optional[datetime.datetime] = None  # bot sends (internal or external)
        self.last_activity_time: Optional[datetime.datetime] = None     # user or bot

        # Night shutdown window (after farewell >= 20:00)
        self.night_shutdown_until: Optional[datetime.datetime] = None

        # A remembered chat for day-bound scheduling
        self.default_chat_id: Optional[int] = None

        # Jobs
        self.jobs: Dict[str, Optional[Job]] = {
            'morning': None,
            'evening': None,
            'followup': None,
            'gambler': None,
            'idle': None,
            'midnight_reset': None,
        }

        # Follow-up anchor (ISO string for serialization)
        self.followup_anchor_user_time: Optional[datetime.datetime] = None

        # Flag to prevent rescheduling for the current turn, used with 'rghtway'
        self.prevent_reschedule_current_turn: bool = False

        self._schedule_midnight_reset_job()

    # -------- Public API (bot.py should use these) --------

    def ensure_daily_schedules(self, chat_id: int):
        """Schedule today's morning/evening (idempotent)."""
        self.default_chat_id = chat_id
        self._ensure_day_rollover()
        self._schedule_morning_message(chat_id)
        self._schedule_evening_message(chat_id)

    def notify_user_message(self, message: str, chat_id: int) -> bool:
        """
        Entry point from bot.py on any inbound user text/voice/photo:
        - Sets anchors and resets chains.
        - Farewell handling (before 20:00 ask 'почему так рано', after 20:00 full shutdown).
        Note: no automatic immediate reply here; the LLM path should reply.
        
        Returns:
            bool: True if the LLM reply should be suppressed (e.g., because a pre-20:00 farewell nudge was sent)
        """
        self.default_chat_id = chat_id
        self._ensure_day_rollover()
        now = self.get_user_time()
        self.last_user_message_time = now
        self.last_activity_time = now

        # Cancel follow-up chain - DON'T schedule here
        self._cancel_job('followup')
        self._cancel_job('gambler')
        self.followup_anchor_user_time = None  # Clear anchor

        # Farewell handling
        if self._is_farewell_message(message or ""):
            if now.hour >= 20:
                self._shutdown_for_night_until_next_8am()
                return False
            self._schedule_immediate_send(chat_id, random.choice([
                "Почему так рано спать?",
                "Уже так рано спать?",
                "ещё ведь рано",
                "Почему так рано ложишься?",
            ]))
            self._schedule_idle_timer(chat_id)
            return True  # Suppress LLM reply
        
        
        return False  # Allow LLM reply

    def is_sending_allowed_now(self) -> bool:
        """For bot.py: whether an LLM reply should be allowed (honor active hours and shutdown)."""
        return (not self._is_shutdown_now()) and self._is_within_active_hours()

    def register_external_send(self, chat_id: Optional[int] = None, schedule_followup: bool = True, prevent_reschedule: bool = False):
        """
        Call after any message sent outside this manager (LLM replies, stickers, etc.)
        to align idle timing and 'time-attached' guards.
        Fix: also cancel pending follow-up/gambler chain and clear anchor so no "why no answer?" after a bot reply.
        If schedule_followup is True, schedule followup and idle timers (for text/photo/voice replies). If False, only update activity timestamps and do not schedule followup/idle (for stickers/commands).
        """
        now = self.get_user_time()
        self.last_bot_message_time = now
        self.last_activity_time = now

        # Cancel follow-up chain and clear anchor on any external send
        self._cancel_job('followup')
        self._cancel_job('gambler')

        if (chat_id or self.default_chat_id) and schedule_followup and not prevent_reschedule and not self.prevent_reschedule_current_turn:
            # Set anchor to BOT reply time
            self.followup_anchor_user_time = now
            self._schedule_followup_attempt(chat_id or self.default_chat_id)
            self._schedule_idle_timer(chat_id or self.default_chat_id)
        else:
            # For stickers/commands, don't restart followup chain
            self.followup_anchor_user_time = None

    def on_reset_command(self):
        """Hard reset of all state and jobs; keeps midnight reset rolling."""
        self._cancel_all_jobs()
        self.current_day = self.get_user_time().date()
        self.quota_remaining = self.DAILY_QUOTA
        self.morning_sent_today = False
        self.evening_sent_today = False
        self.gamble_attempts_today = 0
        self.last_user_message_time = None
        self.last_bot_message_time = None
        self.last_activity_time = None
        self.night_shutdown_until = None
        self.followup_anchor_user_time = None
        self.prevent_reschedule_current_turn = False
        self._schedule_midnight_reset_job()
        if self.default_chat_id is not None:
            self._schedule_morning_message(self.default_chat_id)
            self._schedule_evening_message(self.default_chat_id)



    async def trigger_all_pending_awake_messages(self, context, chat_id: int):
        """
        Immediately send all pending awake messages (idle, followup, gambler) for the given chat_id.
        This is used when the 'rghtway' keyword is detected in a reply, to force all scheduled automation to fire now.
        """
        # Helper to run a callback instantly if job is scheduled
        async def _run_job_now(job_key, callback):
            job = self.jobs.get(job_key)
            if job is not None:
                # Simulate the callback as if timer fired
                # Make sure the job's context.job.data has 'manager' set to self
                job.job_queue = self.job_queue # Ensure job has a job_queue to prevent issues
                job.data['manager'] = self # Ensure manager is correctly passed
                fake_context = type('FakeContext', (object,), {'job': job})()
                try:
                    await callback(fake_context)
                except Exception as e:
                    self.logger.warning(f"Error running {job_key} callback instantly: {e}")
                self._clear_job(job_key)

        # Await each job directly, so they run before returning
        if self.jobs.get('idle') is not None:
            await _run_job_now('idle', self.idle_message_callback)
        if self.jobs.get('followup') is not None:
            await _run_job_now('followup', self.followup_attempt_callback)
        if self.jobs.get('gambler') is not None:
            await _run_job_now('gambler', self.gambler_attempt_callback)

    async def send_all_awake_messages_now(self, context, chat_id: int):
        """
        Immediately send one followup, one gambler, and one idle nudge in sequence using the same phrase pools,
        bypassing the 15-minute guard but still honoring active hours, shutdown, and quota. Does not spam beyond daily limits.
        This cancels any pending awake jobs before sending.
        """
        # Cancel all awake jobs
        self._cancel_job('followup')
        self._cancel_job('gambler')
        self._cancel_job('idle')

        # Helper to send a message if allowed
        async def try_send(texts, require_15min_guard=False):
            if self._is_shutdown_now() or not self._is_within_active_hours() or not self._enforce_quota():
                return False
            text = random.choice(texts)
            await self._send_text(context, chat_id, text)
            return True

        # Send followup
        followup_texts = [
            "чего не отвечаешь?",
            "все норм? чего не отвечаешь?)",
            "ты точно тут?",
            "Ты тут?",
            "чего молчишь?)",
            "что то ты давно не отвечаешь. как твои дела?",
        ]
        await try_send(followup_texts, require_15min_guard=False)

        # Send gambler
        gambler_texts = [
            "чего не отвечаешь?",
            "ты занят?",
            "Ты тут?",
            "Ты тут?)",
            "я онлайн если что.",
            "ты чего молчишь?)",
        ]
        await try_send(gambler_texts, require_15min_guard=False)

        # Send idle
        idle_texts = [
            "Пропала тишина... и ты тоже 🙂",
            "Я тут, если что.",
            "Если ты занята, я подожду.",
            "Напиши, когда будет время.",
            "Я рядом. Не теряйся.",
        ]
        text = None
        if self.llm_generate_idle:
            prompt = "The user hasn’t replied for a long time. Write an engaging message to boost or re-start the conversation. You can ask a thought-provoking question (like 'Что думаешь о Карле Марксе?, '; 'С чем тебе эффективнее учиться - с книгами или с чат-ботами?', 'если бы ты был планетой, то какой? кроме Земли'), share some news about yourself or what you did, or follow up on any event the user mentioned (rarely). Keep it natural, relevant, and interesting. Do not focus purely on questions, make it 50/50. Match previous conversation language."
            try:
                text = await self.llm_generate_idle(chat_id, prompt)
            except Exception as e:
                self.logger.warning(f"LLM idle message generation failed in send_all_awake_messages_now: {e}")
                text = None
        if not text:
            text = random.choice(idle_texts)
        await self._send_text(context, chat_id, text)

    # -------- Internal scheduling --------

    def _schedule_midnight_reset_job(self):
        now = self.get_user_time()
        tomorrow = now.date() + datetime.timedelta(days=1)
        next_midnight = datetime.datetime.combine(tomorrow, datetime.time(0, 0, 0))
        delay = max(1, int((next_midnight - now).total_seconds()))
        self._replace_job('midnight_reset', self.job_queue.run_once(
            self._midnight_reset_callback,
            when=delay,
            data={'manager': self}
        ))




    def _schedule_morning_message(self, chat_id: int):
        if self.morning_sent_today or self.jobs['morning'] is not None:
            self.logger.info(f"Morning message already sent or scheduled. Skipping.")
            return
        now = self.get_user_time()
        if now.hour >= 9:
            self.logger.info(f"Current hour {now.hour} >= 9. Skipping morning message.")
            return

        if now.hour < 8:
            base = now.replace(hour=8, minute=0, second=0, microsecond=0)
            offset = random.randint(0, 59 * 60 + 59)
            scheduled_time = base + datetime.timedelta(seconds=offset)
            self.logger.info(f"Scheduling morning message at random time after 8:00: {scheduled_time} (offset {offset} sec)")
        else:
            delay_min = random.randint(1, 30)
            scheduled_time = now + datetime.timedelta(minutes=delay_min)
            nine = now.replace(hour=9, minute=0, second=0, microsecond=0)
            self.logger.info(f"Scheduling morning message in {delay_min} min at {scheduled_time}")
            if scheduled_time >= nine:
                self.logger.info(f"Scheduled time {scheduled_time} >= 9:00. Skipping.")
                return

        delay = max(1, int((scheduled_time - now).total_seconds()))
        self.logger.info(f"Final morning message delay: {delay} sec")
        self._replace_job('morning', self.job_queue.run_once(
            self.morning_message_callback,
            when=delay,
            data={'chat_id': chat_id, 'manager': self}
        ))

    def _schedule_evening_message(self, chat_id: int):
        if self.evening_sent_today or self.jobs['evening'] is not None:
            self.logger.info(f"Evening message already sent or scheduled. Skipping.")
            return
        now = self.get_user_time()
        if now.hour >= 22:
            self.logger.info(f"Current hour {now.hour} >= 22. Skipping evening message.")
            return

        if now.hour < 19:
            base = now.replace(hour=19, minute=0, second=0, microsecond=0)
            offset = random.randint(0, 3 * 60 * 60 - 1)  # up to 21:59:59
            scheduled_time = base + datetime.timedelta(seconds=offset)
            self.logger.info(f"Scheduling evening message at random time after 19:00: {scheduled_time} (offset {offset} sec)")
        else:
            latest = now.replace(hour=22, minute=0, second=0, microsecond=0)
            max_delay = int((latest - now).total_seconds()) - 1
            if max_delay <= 0:
                self.logger.info(f"No time left for evening message today.")
                return
            delay = random.randint(1, max_delay)
            scheduled_time = now + datetime.timedelta(seconds=delay)
            self.logger.info(f"Scheduling evening message in {delay} sec at {scheduled_time}")

        delay = max(1, int((scheduled_time - now).total_seconds()))
        self.logger.info(f"Final evening message delay: {delay} sec")
        self._replace_job('evening', self.job_queue.run_once(
            self.evening_message_callback,
            when=delay,
            data={'chat_id': chat_id, 'manager': self}
        ))

    def _schedule_immediate_send(self, chat_id: int, text: str):
        self.job_queue.run_once(
            self._send_text_callback,
            when=0,
            data={'chat_id': chat_id, 'text': text, 'manager': self, 'require_15min_guard': False}
        )

    def _schedule_followup_attempt(self, chat_id: int):
        if self.gamble_attempts_today >= len(self.GAMBLE_ATTEMPT_PROBS):
            self.logger.info(f"No followup attempts left today.")
            return
        delay = random.randint(self.FOLLOWUP_FIRST_DELAY_MIN_SEC, self.FOLLOWUP_FIRST_DELAY_MAX_SEC)
        self.logger.info(f"Scheduling followup attempt in {delay} sec (range {self.FOLLOWUP_FIRST_DELAY_MIN_SEC}-{self.FOLLOWUP_FIRST_DELAY_MAX_SEC})")
        self._replace_job('followup', self.job_queue.run_once(
            self.followup_attempt_callback,
            when=delay,
            data={
                'chat_id': chat_id,
                'manager': self,
                'anchor_user_time': self.followup_anchor_user_time.isoformat() if self.followup_anchor_user_time else None
            }
        ))

    def _schedule_next_gambler_attempt(self, chat_id: int):
        if self.gamble_attempts_today >= len(self.GAMBLE_ATTEMPT_PROBS):
            self.logger.info(f"No gambler attempts left today.")
            return
        delay = random.randint(self.GAMBLE_DELAY_MIN_SEC, self.GAMBLE_DELAY_MAX_SEC)
        self.logger.info(f"Scheduling next gambler attempt in {delay} sec (range {self.GAMBLE_DELAY_MIN_SEC}-{self.GAMBLE_DELAY_MAX_SEC})")
        self._replace_job('gambler', self.job_queue.run_once(
            self.gambler_attempt_callback,
            when=delay,
            data={
                'chat_id': chat_id,
                'manager': self,
                'anchor_user_time': self.followup_anchor_user_time.isoformat() if self.followup_anchor_user_time else None
            }
        ))

    def _schedule_idle_timer(self, chat_id: int):
        # Always use the latest user_id if possible
        user_id = chat_id  # fallback if no mapping
        if hasattr(self, 'user_id'):
            user_id = self.user_id
        job = self.job_queue.run_once(
            self.idle_message_callback,
            when=random.randint(self.IDLE_DELAY_MIN_SEC, self.IDLE_DELAY_MAX_SEC),
            data={'manager': self, 'chat_id': chat_id, 'user_id': user_id},
            name=f'idle_{chat_id}'
        )
        self._replace_job('idle', job)

    # -------- Callbacks --------

    @staticmethod
    async def _midnight_reset_callback(context: CallbackContext):
        self: "AwakeMessageManager" = context.job.data['manager']
        self._do_daily_reset()
        self._schedule_midnight_reset_job()
        if self.default_chat_id is not None:
            self._schedule_morning_message(self.default_chat_id)
            self._schedule_evening_message(self.default_chat_id)

    @staticmethod
    async def morning_message_callback(context: CallbackContext):
        self: "AwakeMessageManager" = context.job.data['manager']
        chat_id = context.job.data['chat_id']
        try:
            self._ensure_day_rollover()
            if self.morning_sent_today or self._is_shutdown_now():
                self.logger.info(f"Morning message: already sent or shutdown active. Skipping.")
                return
            if not self._is_within_active_hours() or not self._time_attached_guard_ok():
                self.logger.info(f"Morning message: not within active hours or guard failed. Skipping.")
                return
            rand_val = random.random()
            self.logger.info(f"Morning message: random value {rand_val}, threshold {self.MORNING_PROB}")
            if rand_val < self.MORNING_PROB and self._enforce_quota():
                rand_hello = random.random()
                self.logger.info(f"Morning message: hello random value {rand_hello}, threshold {self.MORNING_HELLO_PROB}")
                text = "Доброе утро!" if rand_hello < self.MORNING_HELLO_PROB else random.choice([
                    "Утро!",
                    "Как спалось?",
                    "С добрым утром!",
                    "Доброе утро! Как настроение?",
                    "Проснулась! Как дела?",
                    "Утречко! Что планируешь на день?",
                ])
                self.logger.info(f"Morning message: sending '{text}'")
                await self._send_text(context, chat_id, text)
                self.morning_sent_today = True
        finally:
            self._clear_job('morning')

    @staticmethod
    async def evening_message_callback(context: CallbackContext):
        self: "AwakeMessageManager" = context.job.data['manager']
        chat_id = context.job.data['chat_id']
        try:
            self._ensure_day_rollover()
            if self.evening_sent_today or self._is_shutdown_now():
                self.logger.info(f"Evening message: already sent or shutdown active. Skipping.")
                return
            if not self._is_within_active_hours() or not self._time_attached_guard_ok():
                self.logger.info(f"Evening message: not within active hours or guard failed. Skipping.")
                return
            rand_val = random.random()
            self.logger.info(f"Evening message: random value {rand_val}, threshold {self.EVENING_PROB}")
            if rand_val < self.EVENING_PROB and self._enforce_quota():
                text = random.choice([
                    "Как прошёл день?",
                    "Чем занимался сегодня?",
                    "Что хорошего было за день?",
                    "Как настроение вечером?",
                    "Как день?",
                    "Устал сегодня?",
                    "Что интересного было?",
                ])
                self.logger.info(f"Evening message: sending '{text}'")
                await self._send_text(context, chat_id, text)
                self.evening_sent_today = True
        finally:
            self._clear_job('evening')

    @staticmethod
    async def _send_text_callback(context: CallbackContext):
        self: "AwakeMessageManager" = context.job.data['manager']
        chat_id = context.job.data['chat_id']
        text = context.job.data['text']
        require_15min_guard = context.job.data.get('require_15min_guard', False)
        await self._try_send_text(context, chat_id, text, require_15min_guard=require_15min_guard)

    @staticmethod
    async def followup_attempt_callback(context: CallbackContext):
        self: "AwakeMessageManager" = context.job.data['manager']
        chat_id = context.job.data['chat_id']
        anchor_iso = context.job.data.get('anchor_user_time')
        try:
            self._ensure_day_rollover()
            self._clear_job('followup')

            if self._is_shutdown_now():
                self.logger.info(f"Followup: shutdown active. Skipping.")
                return
            if not self._anchor_still_valid(anchor_iso):
                self.logger.info(f"Followup: anchor not valid. Skipping.")
                return
            if self.gamble_attempts_today >= len(self.GAMBLE_ATTEMPT_PROBS):
                self.logger.info(f"Followup: no attempts left. Skipping.")
                return

            prob = self.GAMBLE_ATTEMPT_PROBS[self.gamble_attempts_today]
            self.logger.info(f"Followup: attempt {self.gamble_attempts_today+1}, probability {prob}")
            self.gamble_attempts_today += 1  # consume attempt window

            if self._is_within_active_hours() and self._enforce_quota():
                rand_val = random.random()
                self.logger.info(f"Followup: random value {rand_val}, threshold {prob}")
                if rand_val < prob:
                    text = random.choice([
                        "Почему не отвечаешь?",
                        "Эй, ты где пропала?",
                        "Алло? Пропала?",
                        "Ты там?",
                        "Ну что молчишь?",
                        "Куда пропал?",
                    ])
                    self.logger.info(f"Followup: sending '{text}'")
                    await self._try_send_text(context, chat_id, text, require_15min_guard=True)

            if self._anchor_still_valid(anchor_iso) and self.gamble_attempts_today < len(self.GAMBLE_ATTEMPT_PROBS):
                self.logger.info(f"Followup: scheduling next gambler attempt.")
                self._schedule_next_gambler_attempt(chat_id)
        finally:
            pass

    @staticmethod
    async def gambler_attempt_callback(context: CallbackContext):
        self: "AwakeMessageManager" = context.job.data['manager']
        chat_id = context.job.data['chat_id']
        anchor_iso = context.job.data.get('anchor_user_time')
        try:
            self._ensure_day_rollover()
            self._clear_job('gambler')

            if self._is_shutdown_now():
                self.logger.info(f"Gambler: shutdown active. Skipping.")
                return
            if not self._anchor_still_valid(anchor_iso):
                self.logger.info(f"Gambler: anchor not valid. Skipping.")
                return
            if self.gamble_attempts_today >= len(self.GAMBLE_ATTEMPT_PROBS):
                self.logger.info(f"Gambler: no attempts left. Skipping.")
                return

            prob = self.GAMBLE_ATTEMPT_PROBS[self.gamble_attempts_today]
            self.logger.info(f"Gambler: attempt {self.gamble_attempts_today+1}, probability {prob}")
            self.gamble_attempts_today += 1

            if self._is_within_active_hours() and self._enforce_quota():
                rand_val = random.random()
                self.logger.info(f"Gambler: random value {rand_val}, threshold {prob}")
                if rand_val < prob:
                    text = random.choice([
                        "Почему не отвечаешь?",
                        "Эй, ты где пропала?",
                        "Алло? Пропала?",
                        "Ты там?",
                        "Ну что молчишь?",
                        "Куда пропал?",
                    ])
                    self.logger.info(f"Gambler: sending '{text}'")
                    await self._try_send_text(context, chat_id, text, require_15min_guard=True)

            if self._anchor_still_valid(anchor_iso) and self.gamble_attempts_today < len(self.GAMBLE_ATTEMPT_PROBS):
                self.logger.info(f"Gambler: scheduling next gambler attempt.")
                self._schedule_next_gambler_attempt(chat_id)
        finally:
            pass

    @staticmethod
    async def idle_message_callback(context: CallbackContext):
        self: "AwakeMessageManager" = context.job.data['manager']
        chat_id = context.job.data['chat_id']
        user_id = context.job.data.get('user_id') if 'user_id' in context.job.data else chat_id  # fallback
        try:
            self._ensure_day_rollover()
            self._clear_job('idle')

            if self._is_shutdown_now():
                return
            if self.last_activity_time is None:
                return

            now = self.get_user_time()
            if (now - self.last_activity_time).total_seconds() < self.IDLE_DELAY_MIN_SEC:
                self._schedule_idle_timer(chat_id)
                return

            if self._is_within_active_hours() and self._enforce_quota():
                text = None
                if self.llm_generate_idle:
                    prompt = "Пользователь долго тебе не отвечал. Напиши что нибудь интересное на основе диалога чтобы поддержать диалог. Something that could re-engage user, something interesting or provoking."
                    # Optionally, pass context if available (here just a stub)
                    context_str = ""
                    try:
                        text = await self.llm_generate_idle(user_id, prompt)
                    except Exception as e:
                        self.logger.warning(f"LLM idle message generation failed: {e}")
                        text = None
                if not text:
                    text = random.choice([
                        "Пропала тишина... и ты тоже 🙂",
                        "Я тут, если что.",
                        "Если ты занята, я подожду.",
                        "Напиши, когда будет время.",
                        "Я рядом. Не теряйся.",
                    ])
                # Fix: enforce 15-minute guard for idle nudges
                await self._try_send_text(context, chat_id, text, require_15min_guard=True)

            self._schedule_idle_timer(chat_id)
        finally:
            pass

    # -------- Core send helpers (automation only) --------

    async def _try_send_text(self, context: CallbackContext, chat_id: int, text: str, require_15min_guard: bool):
        self._ensure_day_rollover()
        if self._is_shutdown_now():
            return
        if not self._is_within_active_hours():
            return
        if require_15min_guard and not self._time_attached_guard_ok():
            return
        if not self._enforce_quota():
            return
        await self._send_text(context, chat_id, text)

    async def _send_text(self, context: CallbackContext, chat_id: int, text: str):
        await send_message_with_retry(
            context.bot,
            chat_id,
            text=text,
            log_context={"source": "awake_manager", "chat_id": chat_id},
        )
        now = self.get_user_time()
        self.last_bot_message_time = now
        self.last_activity_time = now
        # Mirror into LLM context
        try:
            if self.on_automation_send:
                self.on_automation_send(chat_id, text)
        except Exception:
            pass
        self._schedule_idle_timer(chat_id)

    # -------- Day/Quota/Hours helpers --------

    def _enforce_quota(self) -> bool:
        self._ensure_day_rollover()
        if self.quota_remaining <= 0:
            return False
        self.quota_remaining -= 1
        return True

    def _is_within_active_hours(self) -> bool:
        now = self.get_user_time()
        return self.ACTIVE_START_HOUR <= now.hour < self.ACTIVE_END_HOUR

    def _time_attached_guard_ok(self) -> bool:
        if self.last_bot_message_time is None:
            return True
        now = self.get_user_time()
        return (now - self.last_bot_message_time).total_seconds() >= 15 * 60

    def _ensure_day_rollover(self):
        now_day = self.get_user_time().date()
        if now_day != self.current_day:
            self._do_daily_reset()
            if self.default_chat_id is not None:
                self._schedule_morning_message(self.default_chat_id)
                self._schedule_evening_message(self.default_chat_id)

    def _do_daily_reset(self):
        self.current_day = self.get_user_time().date()
        self.quota_remaining = self.DAILY_QUOTA
        self.morning_sent_today = False
        self.evening_sent_today = False
        self.gamble_attempts_today = 0
        self.night_shutdown_until = None
        for key in ['morning', 'evening', 'followup', 'gambler', 'idle']:
            self._cancel_job(key)
        self.followup_anchor_user_time = None

    # -------- Job bookkeeping --------

    def _replace_job(self, key: str, job: Job):
        self._cancel_job(key)
        self.jobs[key] = job

    def _cancel_job(self, key: str):
        job = self.jobs.get(key)
        if job is not None:
            try:
                job.schedule_removal()
            except JobLookupError:
                # Job already removed, ignore
                pass
            except AttributeError:
                try:
                    job.remove()
                except JobLookupError:
                    pass
                except Exception:
                    self.logger.warning("Failed to cancel job '%s'", key, exc_info=True)
            except Exception:
                self.logger.warning("Failed to cancel job '%s'", key, exc_info=True)
        self.jobs[key] = None

    def _cancel_all_jobs(self):
        for k in list(self.jobs.keys()):
            self._cancel_job(k)

    def _clear_job(self, key: str):
        self.jobs[key] = None

    # -------- Utility logic --------

    def _is_shutdown_now(self) -> bool:
        if self.night_shutdown_until is None:
            return False
        return self.get_user_time() < self.night_shutdown_until

    def _shutdown_for_night_until_next_8am(self):
        now = self.get_user_time()
        tomorrow = now.date() + datetime.timedelta(days=1)
        next_8am = datetime.datetime.combine(tomorrow, datetime.time(self.ACTIVE_START_HOUR, 0, 0))
        self.night_shutdown_until = next_8am
        for key in ['morning', 'evening', 'followup', 'gambler', 'idle']:
            self._cancel_job(key)

    def _is_farewell_message(self, message: str) -> bool:
        m = (message or "").strip().lower()
        patterns = [
            r"\bспокойной ночи\b", r"\bдоброй ночи\b", r"\bдо завтра\b", r"\bувидимся завтра\b",
            r"\bпомолчи\b", r"\bмолчи\b", r"\bдо встречи\b", r"\bgood night\b", r"\bsee you tomorrow\b",
            r"\bbye\b", r"\bgn\b",
        ]
        if any(re.search(p, m) for p in patterns):
            return True
        if any(token in m for token in ["споки", "ночь", "добр ночи", "спок ночи", "see ya"]):
            return True
        return False

    def _anchor_still_valid(self, anchor_iso: Optional[str]) -> bool:
        """True if user hasn't sent a newer message since anchor."""
        if anchor_iso is None:
            return False
        try:
            anchor_dt = datetime.datetime.fromisoformat(anchor_iso)
        except Exception:
            return False
        if self.last_user_message_time is None:
            return True
        return self.last_user_message_time <= anchor_dt
